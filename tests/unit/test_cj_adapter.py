"""Fake HTTP contract checks against the audited official CJ schema projection."""

import json
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from pricehunter.core.config import Settings
from pricehunter.domain.feeds import FeedError, FeedProductData, RejectedFeedRow
from pricehunter.providers.feeds.cj import API, FEED_FIELDS, PRODUCT_FIELDS, CJFeedSource
from pricehunter.providers.feeds.streaming import FeedBounds
from tests.unit.test_feed_adapters import Stream, program, transport

FIXTURES = Path(__file__).parents[1] / "fixtures" / "cj"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def page(nodes, total=None, cursor=None):
    return {
        "data": {
            "products": {
                "resultList": nodes,
                "count": len(nodes),
                "totalCount": len(nodes) if total is None else total,
                "nextPage": cursor,
            }
        }
    }


def source(http, **kwargs):
    return CJFeedSource(http, SecretStr("fixture-pat"), "123", "456", **kwargs)


def response(payload):
    return httpx.Response(200, stream=Stream(json.dumps(payload).encode()))


def test_queries_only_use_audited_fields_arguments_and_return_types():
    schema = fixture("schema")["types"]
    assert schema["Query"]["fields"]["products"]["args"]["page"]["name"] == "String"
    assert "nextPage" in schema["Products"]["fields"]

    # Parse fixed selections and fragments; check every selected field and argument,
    # including nested money/shipping/link types, against the official projection.
    def named(type_):
        return type_["name"] or named(type_["ofType"])

    def validate(selection, type_name):
        tokens = re.findall(r"\.\.\.|[_A-Za-z][_A-Za-z0-9]*|[{}():]", selection)
        position = 0

        def consume(current):
            nonlocal position
            while position < len(tokens) and tokens[position] != "}":
                field = tokens[position]
                position += 1
                if field == "...":
                    assert tokens[position] == "on"
                    fragment = tokens[position + 1]
                    position += 3  # on, type, {
                    consume(fragment)
                    position += 1
                    continue
                if field == "__typename":
                    continue
                definition = schema[current]["fields"][field]
                if position < len(tokens) and tokens[position] == "(":
                    assert tokens[position + 1] in definition["args"]
                    while tokens[position] != ")":
                        position += 1
                    position += 1
                if position < len(tokens) and tokens[position] == "{":
                    position += 1
                    consume(named(definition["type"]))
                    position += 1

        consume(type_name)
        assert position == len(tokens)

    validate(PRODUCT_FIELDS, "Product")
    validate(FEED_FIELDS, "ProductFeed")
    for argument in (
        "companyId",
        "partnerIds",
        "adIds",
        "limit",
        "offset",
        "page",
        "includeDeletedProducts",
    ):
        assert argument in schema["Query"]["fields"]["products"]["args"]


async def test_normalization_and_merchant_scoped_graphql_request(redis):
    requests = []
    nodes = [
        fixture(n)
        for n in (
            "electronics",
            "fashion",
            "missing_availability",
            "affiliate_only",
            "invalid_upc",
            "malformed_money",
        )
    ]

    def handler(request):
        requests.append(request)
        return response(page(nodes))

    client, http = transport(redis, handler)
    async with client:
        items = [x async for x in source(http).stream_items(program("cj"))]
    valid = [x for x in items if isinstance(x, FeedProductData)]
    assert len(valid) == 4 and len(items) == 6
    assert valid[0].price == Decimal("319") and valid[0].original_price == Decimal("349")
    assert valid[0].delivery_cost == Decimal("4.99") and valid[0].source_updated_at.tzinfo
    assert valid[1].variant == {
        "size": "M",
        "color": "Blue",
        "material": "Cotton",
        "pattern": "Plain",
        "sizeType": "Regular",
        "sizeSystem": "EU",
    }
    assert valid[1].parent_external_id == "shirt"
    assert valid[2].availability == "unknown" and valid[3].direct_url is None
    request = requests[0]
    assert str(request.url) == API and request.method == "POST"
    assert request.headers["authorization"] == "Bearer fixture-pat"
    query = json.loads(request.content)["query"]
    assert 'partnerIds:["101"]' in query and 'adIds:["501"]' in query
    assert 'linkCode(pid: "456")' in query and "offset:1" in query
    assert "fixture-pat" not in query


async def test_discovery_and_source_version_are_only_account_visibility(redis):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content)["query"])
        return response(fixture("feeds"))

    client, http = transport(redis, handler)
    async with client:
        adapter = source(http)
        refs = await adapter.discover_feeds()
        assert refs[0].merchant_id == "101" and refs[0].currency == "EUR"
        assert refs[0].market_country == "DE"  # advertiser domicile, NOT delivery permission
        assert await adapter.source_version(program("cj")) == "2026-10-01T10:00:00Z"
        wrong = program("cj")
        wrong.external_feed_id = "999"
        with pytest.raises(FeedError, match="feed_not_authorized"):
            await adapter.source_version(wrong)
    assert "partnerIds" not in requests[0] and 'partnerIds:["101"]' in requests[1]


async def test_cursor_pagination_and_valid_empty(redis):
    requests = []

    def handler(request):
        query = json.loads(request.content)["query"]
        requests.append(query)
        return response(fixture("cursor_last" if "page:" in query else "cursor_first"))

    client, http = transport(redis, handler)
    async with client:
        items = [x async for x in source(http, page_size=1).stream_items(program("cj"))]
    assert len(items) == 2 and all(isinstance(x, FeedProductData) for x in items)
    assert 'page:"next-501"' in requests[1] and "offset:" not in requests[1]
    client, http = transport(redis, lambda r: response(fixture("empty")))
    async with client:
        assert [x async for x in source(http).stream_items(program("cj"))] == []


async def test_partial_node_errors_do_not_abort_other_products(redis):
    client, http = transport(redis, lambda r: response(fixture("partial_error")))
    async with client:
        items = [x async for x in source(http).stream_items(program("cj"))]
    assert isinstance(items[0], FeedProductData) and isinstance(items[1], RejectedFeedRow)


@pytest.mark.parametrize(
    "payload,code",
    [
        (fixture("request_error"), "graphql_request_failed"),
        (page([fixture("another_merchant")]), "wrong_advertiser"),
        (page([], 1), "truncated_page"),
        (page([fixture("electronics")], 10), "row_limit"),
        (page([fixture("electronics")], 2, ""), "invalid_page_cursor"),
        (page([fixture("electronics")], 2, "x"), "page_limit"),
        (
            {"data": {"products": {"resultList": [], "count": True, "totalCount": 0}}},
            "inconsistent_page",
        ),
        (page([]) | {"errors": [{}] * 21}, "graphql_error_limit"),
        (page([fixture("electronics")] * 2), "page_size_limit"),
    ],
)
async def test_malformed_or_unbounded_responses_fail_closed(redis, payload, code):
    client, http = transport(redis, lambda r: response(payload), replace(FeedBounds(), rows=5))
    with pytest.raises(FeedError, match=code):
        async with client:
            _ = [
                x async for x in source(http, page_size=1, max_pages=1).stream_items(program("cj"))
            ]


async def test_repeated_cursor_fails_and_offset_pagination_is_one_based(redis):
    client, http = transport(redis, lambda r: response(page([fixture("electronics")], 3, "same")))
    with pytest.raises(FeedError, match="invalid_page_cursor"):
        async with client:
            _ = [x async for x in source(http, page_size=1).stream_items(program("cj"))]
    queries = []

    def handler(request):
        queries.append(json.loads(request.content)["query"])
        return response(page([fixture("electronics")], 2))

    client, http = transport(redis, handler)
    async with client:
        assert len([x async for x in source(http, page_size=1).stream_items(program("cj"))]) == 2
    assert "offset:1" in queries[0] and "offset:2" in queries[1]


@pytest.mark.parametrize("status", list(fixture("http_errors").values()))
async def test_auth_and_rate_errors_are_safe_and_use_shared_network_limit(redis, status):
    client, http = transport(
        redis, lambda r: httpx.Response(status, stream=Stream(b"do-not-leak-secret"))
    )
    with pytest.raises(FeedError, match=f"http_{status}") as error:
        async with client:
            _ = [x async for x in source(http).stream_items(program("cj"))]
    assert "secret" not in str(error.value) and "fixture-pat" not in str(error.value)


async def test_two_merchants_share_one_network_request_budget(redis, monkeypatch):
    calls = []
    client, http = transport(redis, lambda r: response(fixture("feeds")))
    original = http.limiter.check

    async def check(key, **kwargs):
        calls.append((key, kwargs))
        await original(key, **kwargs)

    monkeypatch.setattr(http.limiter, "check", check)
    async with client:
        adapter = source(http)
        await adapter.source_version(program("cj"))
        second = program("cj")
        second.external_merchant_id = "102"
        second.external_feed_id = "999"
        with pytest.raises(FeedError):
            await adapter.source_version(second)
    assert calls == [("feed-http:cj", {"limit": 60, "seconds": 60})] * 2
    assert await redis.get("ph:rate:feed-http:cj") == b"2"


@pytest.mark.parametrize(
    "changes",
    [
        {
            "shipping": {
                "country": "BE",
                "postalCode": "1000",
                "price": {"amount": "4.99", "currency": "EUR"},
            }
        },
        {
            "shipping": {
                "country": "BE",
                "region": "Brussels",
                "price": {"amount": "4.99", "currency": "EUR"},
            }
        },
    ],
)
def test_scoped_delivery_is_not_fabricated_as_market_wide(changes):
    assert (
        CJFeedSource.normalize(fixture("electronics") | changes, program("cj")).delivery_cost
        is None
    )


def test_other_market_and_merchant_fixtures_keep_strong_id_and_market_context():
    another = program("cj")
    another.external_merchant_id, another.external_feed_id = "102", "502"
    assert (
        CJFeedSource.normalize(fixture("another_merchant"), another).gtin
        == fixture("electronics")["gtin"]
    )
    german = program("cj")
    german.market_country = "DE"
    assert CJFeedSource.normalize(fixture("german"), german).price == 299
    with pytest.raises(ValueError, match="Different target market"):
        CJFeedSource.normalize(fixture("german"), program("cj"))


def test_disabled_default_and_incomplete_enabled_configuration():
    assert not Settings(_env_file=None).cj_enabled
    with pytest.raises(ValueError, match="CJ"):
        Settings(_env_file=None, cj_enabled=True)
    for company, website in (("", "1"), ("1", ""), ('1") { hostile', "2")):
        with pytest.raises(ValueError, match="CJ"):
            CJFeedSource(None, SecretStr("fixture-pat"), company, website)


async def test_query_size_response_depth_and_page_size_are_bounded(redis):
    client, http = transport(redis, lambda r: response(page([])))
    async with client:
        with pytest.raises(FeedError, match="query_size_limit"):
            await source(http)._query("products", {"page": "x" * 9000}, "count")
    client, http = transport(
        redis, lambda r: response({"data": [[[[[1]]]]]}), replace(FeedBounds(), json_depth=3)
    )
    async with client:
        with pytest.raises(FeedError, match="json_nesting_limit"):
            await source(http)._query("products", {}, "count")
    client, http = transport(redis, lambda r: httpx.Response(200, stream=Stream(b" " * 4_000_001)))
    async with client:
        with pytest.raises(FeedError, match="page_size_limit"):
            await source(http)._query("products", {}, "count")
