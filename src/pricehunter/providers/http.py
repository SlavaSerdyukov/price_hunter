import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx

from pricehunter.core.security import validate_url
from pricehunter.domain.errors import (
    ProductNotFoundError,
    ProviderHTTPError,
    ProviderUnavailableError,
)


class ProviderHTTP:
    """Only fixed API endpoints; never use a retailer response's arbitrary href."""

    def __init__(self, client: httpx.AsyncClient, *, timeout: int, max_bytes: int) -> None:
        self.client = client
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def json(
        self,
        method: str,
        url: str,
        *,
        domains: set[str],
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: httpx.BasicAuth | None = None,
    ) -> dict[str, Any]:
        result = await self._read_json(
            method,
            url,
            domains=domains,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            auth=auth,
        )
        if not isinstance(result, dict):
            raise ProviderUnavailableError()
        return result

    async def json_list(
        self,
        url: str,
        *,
        domains: set[str],
        params: dict[str, str] | None = None,
    ) -> list[Any]:
        result = await self._read_json("GET", url, domains=domains, params=params)
        if not isinstance(result, list):
            raise ProviderUnavailableError()
        return result

    async def _error_codes(self, response: httpx.Response) -> tuple[int, ...]:
        """Keep only numeric provider codes, never messages, URLs or error payloads."""
        budget = min(self.max_bytes, 64_000)
        try:
            if response.headers.get("content-encoding", "identity") != "identity":
                return ()
            if int(response.headers.get("content-length", "0")) > budget:
                return ()
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > budget:
                    return ()
            data = json.loads(payload)
            errors = data.get("errors") if isinstance(data, dict) else None
            if not isinstance(errors, list):
                return ()
            return tuple(
                error["errorId"]
                for error in errors[:20]
                if isinstance(error, dict)
                and type(error.get("errorId")) is int
                and 0 < error["errorId"] < 2**31
            )
        except (ValueError, httpx.HTTPError):
            return ()

    async def bytes(
        self,
        method: str,
        url: str,
        *,
        domains: set[str],
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: httpx.BasicAuth | None = None,
    ) -> bytes:
        validate_url(url, domains)
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    method,
                    url,
                    headers={
                        "User-Agent": "PriceHunter/0.1",
                        "Accept-Encoding": "identity",
                        **(headers or {}),
                    },
                    params=params,
                    data=data,
                    json=json_body,
                    auth=auth,
                    follow_redirects=False,
                    timeout=self.timeout,
                ) as response:
                    if response.status_code == 404:
                        raise ProductNotFoundError()
                    if response.status_code != 200:
                        codes = (
                            await self._error_codes(response) if response.status_code == 400 else ()
                        )
                        raise ProviderHTTPError(response.status_code, error_codes=codes)
                    # Refuse encoded bodies so decompression cannot allocate beyond
                    # the response-size budget before streaming chunks are checked.
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ProviderUnavailableError()
                    if int(response.headers.get("content-length", "0")) > self.max_bytes:
                        raise ProviderUnavailableError()
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > self.max_bytes:
                            raise ProviderUnavailableError()
                    return bytes(payload)
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            # Do not include URLs, query parameters, response bodies or token values.
            raise ProviderUnavailableError() from exc

    async def _read_json(
        self,
        method: str,
        url: str,
        *,
        domains: set[str],
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: httpx.BasicAuth | None = None,
    ) -> dict[str, Any] | list[Any]:
        payload = await self.bytes(
            method,
            url,
            domains=domains,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
            auth=auth,
        )
        try:
            result = json.loads(payload, parse_float=Decimal)
        except (ValueError, UnicodeError) as exc:
            raise ProviderUnavailableError() from exc
        if not isinstance(result, (dict, list)):
            raise ProviderUnavailableError()
        return result
