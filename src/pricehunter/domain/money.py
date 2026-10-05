from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field


def decimal_input(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("Money must arrive as a decimal string or Decimal")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(decimal_input),
    Field(gt=0, max_digits=18, decimal_places=4, allow_inf_nan=False),
]


def ancillary_input(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError("Ancillary money requires Decimal")
    return value


AncillaryMoney = Annotated[
    Decimal,
    BeforeValidator(ancillary_input),
    Field(
        ge=0, lt=Decimal("100000000000000"), max_digits=18, decimal_places=4, allow_inf_nan=False
    ),
]
