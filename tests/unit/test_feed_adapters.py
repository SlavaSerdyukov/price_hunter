import csv
import gzip
import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from pricehunter.core.config import Settings
from pricehunter.core.limits import RateLimiter
from pricehunter.db.models import MerchantProgram
from pricehunter.domain.feeds import FeedError, FeedProductData, RejectedFeedRow
from pricehunter.providers.feeds.awin import AwinFeedSource
from pricehunter.providers.feeds.streaming import (
    FeedBounds,
    FeedHTTP,
    bounded_json,
    csv_rows,
    decoded_chunks,
)
from pricehunter.providers.feeds.tradedoubler import TradeDoublerFeedSource

FIXTURES = Path(__file__).parents[1] / "fixtures"


class Stream(httpx.AsyncByteStream):
    def __init__(self, data, chunk=127):
        self.data, self.chunk = data, chunk

    async def __aiter__(self):
        for start in range(0, len(self.data), self.chunk):
            yield self.data[start : start + self.chunk]


def program(network="awin"):
    return MerchantProgram(
        id=uuid4(),
        network=network,
        external_merchant_id="101",
        market_country="BE",
        currency="EUR",
        external_feed_id="501",
        display_name="Example shop",
        domain="example.com",
        feed_language="en",
    )


def transport(redis, handler, bounds=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, FeedHTTP(
        client, RateLimiter(redis, Settings(_env_file=None)), bounds or FeedBounds()
    )


async def test_awin_real_csv_gzip_normalization_and_request(redis):
    requests = []
    payload = gzip.compress((FIXTURES / "awin_feed.csv").read_bytes())

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=Stream(payload))

    client, http = transport(redis, handler)
    async with client:
        source = AwinFeedSource(http, SecretStr("fixture-key"))
        items = [r async for r in source.stream_items(program())]
    assert len(items) == 9
    valid = [r for r in items if isinstance(r, FeedProductData)]
    assert len(valid) == 7 and sum(isinstance(r, RejectedFeedRow) for r in items) == 2
    assert valid[0].price == Decimal("329") and valid[0].original_price == Decimal("349")
    assert valid[0].delivery_cost == Decimal("4.99")
    assert valid[0].availability == "in_stock" and valid[0].source_updated_at.tzinfo
    assert len({tuple(sorted(r.variant.items())) for r in valid[1:4]}) == 3
    assert valid[4].upc == "012345678905" and valid[4].availability == "unknown"
    assert valid[4].image_url is None and valid[4].direct_url is None
    assert valid[5].brand is None
    assert requests[0].url.host == "datafeed.api.productserve.com"
    assert "/fid/501/format/csv/" in requests[0].url.path
    assert requests[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "change,code",
    [({"merchant_id": "999"}, "wrong_advertiser"), ({"currency": "USD"}, "wrong_currency")],
)
async def test_awin_systemic_context_errors_fail_sync(redis, change, code):
    rows = list(csv.DictReader(io.StringIO((FIXTURES / "awin_feed.csv").read_text())))
    row = rows[0] | change
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    client, http = transport(
        redis,
        lambda r: httpx.Response(200, stream=Stream(gzip.compress(output.getvalue().encode()))),
    )
    with pytest.raises(FeedError, match=code):
        async with client:
            _ = [r async for r in AwinFeedSource(http, SecretStr("secret")).stream_items(program())]


async def test_awin_feed_list_does_not_return_credential_url(redis):
    payload = b"Advertiser ID,Advertiser Name,Primary Region,Feed ID,Last Imported,URL\n101,Example,BE,501,version-1,https://secret.example/key\n"
    client, http = transport(redis, lambda r: httpx.Response(200, stream=Stream(payload)))
    async with client:
        source = AwinFeedSource(http, SecretStr("secret"))
        refs = await source.discover_feeds()
        assert refs[0].feed_id == "501" and "secret" not in repr(refs)
        assert await source.source_version(program()) == "version-1"
        wrong = program()
        wrong.external_merchant_id = "999"
        with pytest.raises(FeedError, match="feed_not_authorized"):
            await source.source_version(wrong)
        wrong_market = program()
        wrong_market.market_country = "DE"
        with pytest.raises(FeedError, match="wrong_market"):
            await source.source_version(wrong_market)


async def test_tradedoubler_pagination_normalizes_and_checks_source_version(redis):
    requests = []
    page = json.loads((FIXTURES / "tradedoubler_page.json").read_text())

    def handler(request):
        requests.append(request)
        assert request.url.params["token"] == "fixture-products-token"
        path = request.url.path
        if "productFeeds" in path:
            result = {
                "feeds": [
                    {
                        "feedId": 501,
                        "active": True,
                        "visible": True,
                        "currencyISOCode": "EUR",
                        "programs": [{"programId": 101, "name": "Example"}],
                        "lastModifiedTime": "v1",
                    }
                ]
            }
        elif "lastUpdated" in path:
            result = {"feedIds": [501], "lastUpdatedTime": "2026-01-01T10:00:00"}
        else:
            result = json.loads(json.dumps(page))
            result["productHeader"]["totalHits"] = 2
            if ";page=1;" in path:
                result["products"][0]["offers"][0]["sourceProductId"] = "second"
        return httpx.Response(200, stream=Stream(json.dumps(result).encode()))

    client, http = transport(redis, handler)
    async with client:
        source = TradeDoublerFeedSource(http, SecretStr("fixture-products-token"), page_size=1)
        assert (await source.discover_feeds())[0].merchant_id == "101"
        assert await source.source_version(program("tradedoubler")) == "2026-01-01T10:00:00"
        items = [r async for r in source.stream_items(program("tradedoubler"))]
    assert [i.external_id for i in items] == ["sony", "second"]
    assert items[0].price == Decimal("329") and items[0].ean and items[0].mpn
    assert items[0].variant == {"colour": "black"} and items[0].source_updated_at.tzinfo
    assert items[0].original_price is None
    assert all(r.url.scheme == "https" and r.url.host == "api.tradedoubler.com" for r in requests)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("currency", "wrong_currency"),
        ("feed", "wrong_feed"),
        ("truncated", "truncated_page"),
        ("html", "invalid_json"),
        ("schema", "invalid_page"),
        ("page_limit", "page_limit"),
    ],
)
async def test_tradedoubler_systemic_failures(redis, kind, code):
    page = json.loads((FIXTURES / "tradedoubler_page.json").read_text())
    if kind == "currency":
        page["products"][0]["offers"][0]["price"]["currency"] = "USD"
    if kind == "feed":
        page["products"][0]["offers"][0]["feedId"] = 999
    if kind == "truncated":
        page["products"] = []
    if kind == "schema":
        page = {"products": "bad"}
    if kind == "page_limit":
        page["productHeader"]["totalHits"] = 2
    payload = b"<html>Login</html>" if kind == "html" else json.dumps(page).encode()
    client, http = transport(redis, lambda r: httpx.Response(200, stream=Stream(payload)))
    with pytest.raises(FeedError, match=code):
        async with client:
            _ = [
                r
                async for r in TradeDoublerFeedSource(
                    http, SecretStr("secret"), max_pages=1
                ).stream_items(program("tradedoubler"))
            ]


async def test_tradedoubler_bad_row_does_not_lose_valid_rows(redis):
    page = json.loads((FIXTURES / "tradedoubler_page.json").read_text())
    bad = json.loads(json.dumps(page["products"][0]))
    bad["offers"][0]["price"]["value"] = "bad"
    page["products"].append(bad)
    page["productHeader"]["totalHits"] = 2
    client, http = transport(
        redis, lambda r: httpx.Response(200, stream=Stream(json.dumps(page).encode()))
    )
    async with client:
        items = [
            r
            async for r in TradeDoublerFeedSource(http, SecretStr("secret")).stream_items(
                program("tradedoubler")
            )
        ]
    assert isinstance(items[0], FeedProductData) and isinstance(items[1], RejectedFeedRow)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
async def test_feed_http_safe_errors_and_no_redirects(redis, status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status, headers={"location": "https://127.0.0.1/secret"}, stream=Stream(b"private")
        )

    client, http = transport(redis, handler)
    with pytest.raises(FeedError, match=f"http_{status}") as exc:
        async with client:
            _ = [
                r
                async for r in http.stream(
                    "awin", "https://productdata.awin.com/secret", domains={"productdata.awin.com"}
                )
            ]
    assert str(exc.value) == f"http_{status}" and len(seen) == 1


@pytest.mark.parametrize(
    "url", ["http://productdata.awin.com/x", "https://127.0.0.1/x", "https://other.example/x"]
)
async def test_feed_http_rejects_unapproved_destinations(redis, url):
    client, http = transport(redis, lambda r: pytest.fail("Unsafe request reached transport"))
    from pricehunter.domain.errors import InvalidProductUrlError

    with pytest.raises(InvalidProductUrlError):
        async with client:
            _ = [r async for r in http.stream("awin", url, domains={"productdata.awin.com"})]


@pytest.mark.parametrize(
    "kind,code",
    [
        ("truncated", "truncated_gzip"),
        ("members", "multiple_gzip_members"),
        ("bomb", "decompressed_size_limit"),
        ("compressed", "compressed_size_limit"),
        ("invalid", "invalid_gzip"),
    ],
)
async def test_gzip_bounds(kind, code):
    payload = gzip.compress(b"a" * 10000)
    bounds = FeedBounds()
    if kind == "truncated":
        payload = payload[:-5]
    if kind == "members":
        payload += gzip.compress(b"extra")
    if kind == "bomb":
        bounds = replace(bounds, decompressed_bytes=100)
    if kind == "compressed":
        bounds = replace(bounds, compressed_bytes=10)
    if kind == "invalid":
        payload = b"invalid bytes"
    with pytest.raises(FeedError, match=code):
        _ = [r async for r in decoded_chunks(Stream(payload).__aiter__(), bounds, gzip=True)]


async def test_csv_quoted_newlines_utf8_and_record_bounds():
    rows = [
        r
        async for r in csv_rows(
            Stream('id,title\n1,"café\nwith ""quotes"""\n'.encode(), 1).__aiter__(), FeedBounds()
        )
    ]
    assert rows == [{"id": "1", "title": 'café\nwith "quotes"'}]
    for payload, bounds, code in [
        (b'id,title\n1,"open', FeedBounds(), "truncated_csv"),
        (b"id,id\n1,2\n", FeedBounds(), "invalid_header"),
        (
            b"id,title\n1," + b"x" * 200 + b"\n",
            replace(FeedBounds(), record_bytes=100),
            "record_size_limit",
        ),
        (b"id,title\n1,\xff\n", FeedBounds(), "invalid_utf8"),
    ]:
        with pytest.raises(FeedError, match=code):
            _ = [r async for r in csv_rows(Stream(payload).__aiter__(), bounds)]
    with pytest.raises(FeedError, match="json_nesting_limit"):
        bounded_json(b"[" * 30 + b"0" + b"]" * 30, FeedBounds())


def test_sources_require_credentials():
    with pytest.raises(ValueError, match="AWIN_FEED_API_KEY"):
        AwinFeedSource(None, SecretStr(""))
    with pytest.raises(ValueError, match="TRADEDOUBLER_TOKEN"):
        TradeDoublerFeedSource(None, SecretStr(""))


async def test_page_timeout_excludes_shared_rate_limit_wait(redis, monkeypatch):
    import asyncio

    from pricehunter.domain.errors import RateLimitExceededError

    client, http = transport(redis, lambda r: httpx.Response(200, stream=Stream(b'{"ok":true}')))
    http.timeout = 0.01
    attempts = 0
    sleep = asyncio.sleep

    async def check(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitExceededError()

    async def wait(seconds):
        assert seconds == 60
        await sleep(0.03)

    monkeypatch.setattr(http.limiter, "check", check)
    monkeypatch.setattr("pricehunter.providers.feeds.streaming.asyncio.sleep", wait)
    async with client:
        result = await http.json(
            "awin", "https://productdata.awin.com/x", domains={"productdata.awin.com"}
        )
    assert result == {"ok": True} and attempts == 2


@pytest.mark.parametrize("value", [{"value": "x" * 101}, {"x" * 101: 1}])
def test_json_field_budget_applies_to_values_and_keys(value):
    with pytest.raises(FeedError, match="field_size_limit"):
        bounded_json(json.dumps(value).encode(), replace(FeedBounds(), field_chars=100))
