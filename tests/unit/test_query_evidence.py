import pytest

from pricehunter.domain.search import query_evidence_rank
from tests.unit.test_search_relevance import QUERY, offer


@pytest.mark.parametrize(
    "query,changes,expected",
    [
        ("4548736162563", {"gtin": "4548736162563"}, 0),
        ("04548736162563", {"ean": "4548736162563"}, 0),
        ("00012345678905", {"upc": "012345678905"}, 0),
        ("WH_1000XM6", {"model": "WH-1000XM6"}, 1),
        ("WH.1000XM6", {"mpn": "WH1000XM6"}, 1),
        (QUERY, {"brand": "Sony", "model": "WH1000XM6"}, 2),
        (QUERY, {"brand": "SONY", "mpn": "WH-1000XM6"}, 2),
        ("sony headphones", {"title": "Sony Headphones"}, 3),
        ("Sony   Headphones", {"title": "Sony Headphones"}, 3),
        ("headphones", {"title": "Wireless headphones with case"}, 4),
        (QUERY, {"title": "Other product"}, 5),
        ("", {"title": QUERY, "brand": "Sony", "model": "WH1000XM6"}, 5),
        ("  ", {"title": QUERY}, 5),
        ("WH1000XM6+", {"model": "WH1000XM6"}, 5),
        ("WH1000XM6/2", {"model": "WH1000XM6"}, 5),
    ],
)
def test_query_evidence_priority_uses_existing_normalization(query, changes, expected):
    assert query_evidence_rank(offer("listing", **changes), query) == expected


@pytest.mark.parametrize(
    "query", ["123", "4548736162564", "+4548736162563", "４５４８７３６１６２５６３"]
)
def test_invalid_or_non_ascii_numeric_query_cannot_get_trade_identifier_boost(query):
    assert query_evidence_rank(offer("listing", gtin="4548736162563"), query) == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"gtin": "4006381333931", "model": "4548736162563", "title": "4548736162563"},
        {"gtin": "4548736162563", "ean": "4006381333931"},
    ],
)
def test_conflicting_valid_trade_identifiers_cannot_get_an_exact_query_boost(changes):
    assert query_evidence_rank(offer("listing", **changes), "4548736162563") == 5


def test_invalid_incoming_identifier_fails_closed_even_if_model_copy_skipped_validation():
    data = offer("listing").model_copy(update={"gtin": "4548736162564"})
    assert query_evidence_rank(data, "4548736162563") == 5


def test_listing_ids_prices_and_commercial_metadata_cannot_change_evidence():
    data = offer("a", title=QUERY, brand="Sony", model="WH1000XM6")
    changed = data.model_copy(
        update={
            "external_id": "zzz",
            "store_slug": "aaa",
            "provider": "cj",
            "affiliate_network": "awin",
            "affiliate_metadata": {"commission": 999999, "payout": 100},
            "metadata": {"commission": 999999, "affiliate_program": "preferred"},
            "price": "999999",
        }
    )
    assert query_evidence_rank(data, QUERY) == query_evidence_rank(changed, QUERY) == 2
