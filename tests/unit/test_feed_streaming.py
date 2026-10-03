import pytest


@pytest.mark.parametrize("count", [1000, 100000])
async def test_csv_streams_large_feeds_with_bounded_buffer(count):
    from pricehunter.providers.feeds.streaming import FeedBounds, csv_rows

    produced = 0

    async def chunks():
        nonlocal produced
        yield b"id,title,price\n"
        for i in range(count):
            produced += 1
            yield f'{i},"Example product {i}",329.00\n'.encode()

    consumed = 0
    async for item in csv_rows(chunks(), FeedBounds()):
        consumed += 1
        assert produced - consumed <= 1
        assert item["price"] == "329.00"
    assert consumed == count
