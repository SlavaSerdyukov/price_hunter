from typing import Annotated

from babel import Locale
from pydantic import AfterValidator, Field


def validate_country(value: str) -> str:
    if len(value) != 2 or not value.isascii() or not value.isupper():
        raise ValueError("Expected ISO 3166-1 alpha-2 country")
    if value not in Locale("en").territories or value in {"ZZ", "EU", "UN", "XA", "XB"}:
        raise ValueError("Unknown country")
    return value


CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$"), AfterValidator(validate_country)]
MARKET_CHOICES = ("BE", "DE", "FR", "NL", "IT", "ES", "AT", "IE", "PL", "GB", "US", "CA")
