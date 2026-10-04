from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from pricehunter.db.base import utcnow
from pricehunter.domain.products import Money


def evidence(**changes):
    from pricehunter.domain.delivery import DeliveryContext, DeliveryQuoteData

    context = DeliveryContext(country="BE", postal_code="2000")
    return DeliveryQuoteData.model_validate(
        {
            "offer_id": uuid4(),
            "snapshot_key": "a" * 64,
            "country": "BE",
            "scope": "exact",
            "destination_key": context.fingerprint,
            "currency": "EUR",
            "shipping_price": Decimal("10"),
            "tax_status": "included",
            "availability": "in_stock",
            "quoted_at": utcnow(),
            "expires_at": utcnow() + timedelta(minutes=10),
            "source": "mock",
            **changes,
        }
    )


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("0.0001"), Decimal("12.34")])
def test_ancillary_money_accepts_zero_and_bounded_decimals(value):
    from pricehunter.domain.delivery import AncillaryMoney

    assert TypeAdapter(AncillaryMoney).validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        0.0,
        -1.0,
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0.00001"),
        Decimal("100000000000000"),
        "0",
        0,
    ],
)
def test_ancillary_money_rejects_inexact_invalid_or_unbounded_input(value):
    from pricehunter.domain.delivery import AncillaryMoney

    with pytest.raises(ValidationError):
        TypeAdapter(AncillaryMoney).validate_python(value)


def test_item_money_remains_strictly_positive():
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(Decimal("0"))


@pytest.mark.parametrize(
    "status,amount,total",
    [
        ("included", None, "310"),
        ("not_applicable", None, "310"),
        ("additional", Decimal("20"), "330"),
        ("additional", Decimal("0"), "310"),
        ("unknown", None, None),
    ],
)
def test_tax_semantics_have_one_deterministic_total(status, amount, total):
    from pricehunter.domain.delivery import delivered_total

    result = delivered_total(Decimal("300"), "EUR", evidence(tax_status=status, tax_amount=amount))
    assert result == (Decimal(total) if total else None)


def test_missing_additional_tax_is_rejected():
    with pytest.raises(ValidationError):
        evidence(tax_status="additional")


def test_missing_shipping_and_wrong_currency_never_produce_total():
    from pricehunter.domain.delivery import delivered_total

    assert delivered_total(Decimal("300"), "EUR", evidence(shipping_price=None)) is None
    assert delivered_total(Decimal("300"), "GBP", evidence()) is None


def test_postal_normalization_is_conservative_private_and_exact():
    from pricehunter.domain.delivery import DeliveryContext

    a = DeliveryContext(country="NL", postal_code=" 1012   ab ")
    b = DeliveryContext(country="NL", postal_code="1012 AB")
    assert a.postal_code == b.postal_code == "1012 AB"
    assert a.fingerprint == b.fingerprint
    assert "1012" not in repr(a)
    assert a.fingerprint != DeliveryContext(country="NL", postal_code="1012AB").fingerprint
    assert a.fingerprint != DeliveryContext(country="BE", postal_code="1012 AB").fingerprint


@pytest.mark.parametrize("postal", ["", " ", "x" * 21, "2000\n", "2000\x00"])
def test_postal_codes_are_bounded_and_controls_rejected(postal):
    from pricehunter.domain.delivery import DeliveryContext

    with pytest.raises(ValidationError):
        DeliveryContext(country="BE", postal_code=postal)


@pytest.mark.parametrize("country", ["be", "EU", "ZZ", "BEL", "XX"])
def test_destination_uses_existing_iso_validation(country):
    from pricehunter.domain.delivery import DeliveryContext

    with pytest.raises(ValidationError):
        DeliveryContext(country=country)


def test_exact_scope_is_not_fuzzy_and_country_scope_is_explicit():
    from pricehunter.domain.delivery import DeliveryContext

    quote = evidence()
    assert quote.matches(DeliveryContext(country="BE", postal_code="2000"))
    assert not quote.matches(DeliveryContext(country="BE", postal_code="3500"))
    assert not quote.matches(DeliveryContext(country="NL", postal_code="2000"))
    country = evidence(scope="country", destination_key=DeliveryContext(country="BE").fingerprint)
    assert country.matches(DeliveryContext(country="BE", postal_code="3500"))
    assert not country.matches(DeliveryContext(country="NL"))


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": utcnow() - timedelta(hours=1)},
        {"expires_at": utcnow() + timedelta(days=2)},
        {"scope": "exact", "destination_key": None},
        {"scope": "country", "destination_key": "f" * 64},
    ],
)
def test_quote_scope_and_validity_are_bounded(changes):
    with pytest.raises(ValidationError):
        evidence(**changes)
