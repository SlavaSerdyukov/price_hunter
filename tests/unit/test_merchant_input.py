import argparse

import pytest
from pydantic import ValidationError

from pricehunter.apps.merchant_admin import add_commands
from pricehunter.domain.merchants import MerchantInput


@pytest.mark.parametrize(
    "changes",
    [
        {"slug": "UPPER"},
        {"slug": "has space"},
        {"display_name": " "},
        {"primary_domain": "https://example.com"},
        {"api_key": "forbidden"},
    ],
)
def test_identity_document_rejects_invalid_metadata_and_extra_secrets(changes):
    with pytest.raises(ValidationError):
        MerchantInput.model_validate(dict(slug="retailer", display_name="Retailer") | changes)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--confirm"],
        ["--expected-version", "0", "--reason", "Review"],
        ["--expected-version", "0", "--reason", "Review", "--confirm", "--dry-run"],
    ],
)
def test_mutating_cli_requires_receipt_reason_and_one_execution_mode(arguments):
    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["merchant-create", "merchant.json", *arguments])
    assert error.value.code == 2
