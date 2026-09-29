import asyncio
import codecs
import csv
import io
import json
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from pricehunter.core.limits import RateLimiter
from pricehunter.core.security import validate_url
from pricehunter.domain.errors import RateLimitExceededError
from pricehunter.domain.feeds import FeedError


@dataclass(frozen=True)
class FeedBounds:
    compressed_bytes: int = 256_000_000
    decompressed_bytes: int = 2_000_000_000
    record_bytes: int = 131072
    field_chars: int = 16384
    rows: int = 1_000_000
    headers: int = 16384
    json_depth: int = 12


async def decoded_chunks(
    chunks: AsyncIterator[bytes], bounds: FeedBounds, *, gzip: bool
) -> AsyncIterator[bytes]:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip else None
    compressed = decompressed = 0
    try:
        async for raw in chunks:
            compressed += len(raw)
            if compressed > bounds.compressed_bytes:
                raise FeedError("compressed_size_limit")
            if decoder is not None:
                if decoder.eof and raw:
                    raise FeedError("multiple_gzip_members")
                pending = raw
                while pending:
                    value = decoder.decompress(pending, 16384)
                    pending = decoder.unconsumed_tail
                    if decoder.unused_data:
                        raise FeedError("multiple_gzip_members")
                    decompressed += len(value)
                    if decompressed > bounds.decompressed_bytes:
                        raise FeedError("decompressed_size_limit")
                    if value:
                        yield value
            else:
                decompressed += len(raw)
                if decompressed > bounds.decompressed_bytes:
                    raise FeedError("decompressed_size_limit")
                # Buffer size is controlled even if a fake transport supplies large chunks.
                for offset in range(0, len(raw), 16384):
                    yield raw[offset : offset + 16384]
        if decoder is not None and not decoder.eof:
            raise FeedError("truncated_gzip")
    except zlib.error:
        raise FeedError("invalid_gzip") from None


async def csv_rows(
    chunks: AsyncIterator[bytes], bounds: FeedBounds, *, required: set[str] | None = None
) -> AsyncIterator[dict[str, str]]:
    """Incremental logical records, including quoted newlines. Never retain the feed."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    record: list[str] = []
    quoted = False
    size = total = count = 0
    header: list[str] | None = None

    def parse(value: str) -> list[str]:
        try:
            rows = list(csv.reader(io.StringIO(value), strict=True))
        except csv.Error:
            raise FeedError("invalid_csv_record") from None
        if len(rows) != 1 or any(len(v) > bounds.field_chars for v in rows[0]):
            raise FeedError("invalid_csv_record")
        return rows[0]

    try:
        async for raw in chunks:
            total += len(raw)
            if total > bounds.decompressed_bytes:
                raise FeedError("decompressed_size_limit")
            for char in decoder.decode(raw):
                record.append(char)
                size += len(char.encode("utf-8"))
                if size > bounds.record_bytes:
                    raise FeedError("record_size_limit")
                if char == '"':
                    quoted = not quoted
                if char == "\n" and not quoted:
                    values = parse("".join(record))
                    record, size = [], 0
                    if header is None:
                        header = values
                        if (
                            not header
                            or len(set(header)) != len(header)
                            or len(header) > 200
                            or not (required or set()) <= set(header)
                        ):
                            raise FeedError("invalid_header")
                    else:
                        count += 1
                        if count > bounds.rows:
                            raise FeedError("row_limit")
                        if len(values) != len(header):
                            raise FeedError("invalid_csv_record")
                        yield dict(zip(header, values, strict=True))
        decoder.decode(b"", final=True)
        if quoted:
            raise FeedError("truncated_csv")
        if record:
            values = parse("".join(record))
            if header is None or len(values) != len(header):
                raise FeedError("invalid_header")
            if count >= bounds.rows:
                raise FeedError("row_limit")
            yield dict(zip(header, values, strict=True))
        if header is None:
            raise FeedError("empty_feed")
    except UnicodeError:
        raise FeedError("invalid_utf8") from None


def bounded_json(payload: bytes, bounds: FeedBounds) -> Any:
    depth = 0
    quoted = escaped = False
    for char in payload:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > bounds.json_depth:
                raise FeedError("json_nesting_limit")
        elif char in (93, 125):
            depth -= 1
    try:
        # Decimal strings remain strings; network floats are decoded without binary loss.
        from decimal import Decimal

        value = json.loads(payload, parse_float=Decimal)
    except (ValueError, RecursionError):
        raise FeedError("invalid_json") from None

    def check_fields(value: Any) -> None:
        if isinstance(value, str) and len(value) > bounds.field_chars:
            raise FeedError("field_size_limit")
        if isinstance(value, dict):
            for key, field in value.items():
                check_fields(key)
                check_fields(field)
        elif isinstance(value, list):
            for field in value:
                check_fields(field)

    check_fields(value)
    return value


class FeedHTTP:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
        bounds: FeedBounds,
        *,
        timeout: int = 60,
    ) -> None:
        self.client, self.limiter, self.bounds, self.timeout = client, limiter, bounds, timeout

    async def stream(
        self,
        network: str,
        url: str,
        *,
        domains: set[str],
        params: dict[str, str] | None = None,
        gzip: bool = False,
        total_timeout: int | None = None,
    ) -> AsyncIterator[bytes]:
        validate_url(url, domains)
        limit = self.limiter.settings.provider_rate_limits.get(
            network, 5 if network == "awin" else 60
        )
        while True:
            try:
                await self.limiter.check(f"feed-http:{network}", limit=limit, seconds=60)
                break
            except RateLimitExceededError:
                # Shared pacing, not a remote retry. A sync heartbeat owns the lease
                # while waiting, and no DB transaction spans this network operation.
                await asyncio.sleep(60)
        try:
            async with (
                asyncio.timeout(total_timeout),
                self.client.stream(
                    "GET",
                    url,
                    params=params,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                    timeout=self.timeout,
                ) as response,
            ):
                if response.status_code != 200:
                    raise FeedError(f"http_{response.status_code}")
                if sum(len(k) + len(v) for k, v in response.headers.raw) > self.bounds.headers:
                    raise FeedError("headers_size_limit")
                if int(response.headers.get("content-length", "0")) > self.bounds.compressed_bytes:
                    raise FeedError("compressed_size_limit")
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in {"identity", "gzip"}:
                    raise FeedError("unsupported_encoding")
                async for value in decoded_chunks(
                    response.aiter_raw(), self.bounds, gzip=gzip or encoding == "gzip"
                ):
                    yield value
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise FeedError("transport_error") from None

    async def json(
        self, network: str, url: str, *, domains: set[str], params: dict[str, str] | None = None
    ) -> Any:
        payload = bytearray()
        async for chunk in self.stream(
            network, url, domains=domains, params=params, total_timeout=self.timeout
        ):
            payload.extend(chunk)
            if len(payload) > 4_000_000:
                raise FeedError("page_size_limit")
        return bounded_json(bytes(payload), self.bounds)
