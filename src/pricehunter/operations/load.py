"""Bounded authenticated HTTP workloads; reports contain no request paths or identities."""

import asyncio
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from pricehunter.core.container import Container
from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.integrity import integrity, state
from pricehunter.operations.synthetic import Dataset, require_synthetic

ProfileName = Literal["smoke", "beta-small", "beta-medium"]


@dataclass(frozen=True)
class Profile:
    concurrency: int
    requests: int
    users: int


PROFILES: dict[str, Profile] = {
    "smoke": Profile(4, 56, 4),
    "beta-small": Profile(8, 280, 16),
    "beta-medium": Profile(24, 1400, 30),
}
ROUTES = ("comparison", "offers", "search", "settings", "subscription", "history", "delivery")


class Latency(BaseModel):
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


class RouteReport(BaseModel):
    requests: int
    statuses: dict[str, int]
    latency: Latency


class LoadReport(BaseModel):
    profile: str
    concurrency: int
    synthetic_users: int
    requests: int
    successes: int
    expected_rate_limits: int
    unexpected_failures: int
    seconds: float
    requests_per_second: float
    latency: Latency
    routes: dict[str, RouteReport]
    integrity_passed: bool = True
    response_bounds_passed: bool = True
    checked_out_connections: int = Field(ge=0)


def latency(values: list[float]) -> Latency:
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000, 3)

    return Latency(
        p50_ms=percentile(0.5),
        p95_ms=percentile(0.95),
        p99_ms=percentile(0.99),
        max_ms=round(max(ordered) * 1000, 3),
    )


async def run_load(
    container: Container,
    dataset: Dataset,
    client: httpx.AsyncClient,
    profile_name: str,
) -> LoadReport:
    require_synthetic(container)
    if profile_name not in PROFILES:
        raise RecoveryError("load_profile_invalid")
    profile = PROFILES[profile_name]
    keys = dataset.api_keys[1:-1][: profile.users]
    if len(keys) != profile.users:
        raise RecoveryError("load_users_missing")
    before = await state(container.sessions)
    labels: dict[str, list[tuple[int, float]]] = {name: [] for name in ROUTES}
    work = iter(range(profile.requests))
    bounds = True
    rate_limits = 0
    started = time.monotonic()

    async def worker() -> None:
        nonlocal bounds, rate_limits
        for index in work:
            route = ROUTES[index % len(ROUTES)]
            key = keys[(index // len(ROUTES)) % len(keys)]
            base = f"/api/v1/products/{dataset.product_id}"
            method, path = "GET", base
            payload: dict[str, str] | None = None
            params: dict[str, str | int] = {"country": "DE"}
            if route == "offers":
                path, params = base + "/offers", {"country": "DE", "size": 2, "page": index % 2}
            elif route == "search":
                path, params = "/api/v1/search", {"q": "Sony", "country": "DE", "currency": "EUR"}
            elif route == "settings":
                path, params = "/api/v1/users/me/settings", {}
            elif route == "subscription":
                path, params = "/api/v1/subscriptions/me", {}
            elif route == "history":
                path, params = base + "/history", {"offer_id": str(dataset.offer_id), "limit": 5}
            elif route == "delivery":
                method, path, params = (
                    "POST",
                    base + "/delivery-comparison",
                    {"country": "BE", "size": 2},
                )
                payload = {"country": "BE"}
            request_start = time.monotonic()
            try:
                async with asyncio.timeout(30):
                    response = await client.request(
                        method,
                        path,
                        params=params,
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                    )
                status = response.status_code
                if status == 429:
                    if response.json().get("error") == "rate_limit":
                        rate_limits += 1
                    else:
                        status = 499
                if status == 200:
                    body = response.json()
                    if route in ("comparison", "offers", "delivery"):
                        cap = 10 if route == "comparison" else 2
                        bounds &= len(body["offers"]) <= cap
                        bounds &= len(body["delivered_offers"]) <= cap
                    elif route == "search":
                        # All selected users are paid. Enforced server limits are still checked.
                        result_limit = (
                            container.settings.power_search_results
                            if key == dataset.api_keys[2]
                            else container.settings.pro_search_results
                        )
                        bounds &= len(body["products"]) <= result_limit
                        bounds &= (
                            len(body["products"]) <= container.settings.search_comparison_limit
                        )
                        bounds &= all(len(p["offers"]) <= 10 for p in body["products"])
                    elif route == "history":
                        bounds &= len(body["observations"]) <= 5
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError):
                status = 0
            labels[route].append((status, time.monotonic() - request_start))

    async with asyncio.timeout(180):
        await asyncio.gather(*(worker() for _ in range(profile.concurrency)))
    duration = time.monotonic() - started
    await integrity(container.sessions)
    after = await state(container.sessions)
    for table in (
        "payment_events",
        "subscription_periods",
        "notification_events",
        "best_price_events",
        "price_observations",
    ):
        if after[table] != before[table]:
            raise RecoveryError("load_changed_authoritative_history")
    checked_out = container.sessions.kw["bind"].sync_engine.pool.checkedout()
    routes = {
        name: RouteReport(
            requests=len(values),
            statuses=dict(Counter(str(s) for s, _ in values)),
            latency=latency([seconds for _, seconds in values]),
        )
        for name, values in labels.items()
    }
    all_values = [value for values in labels.values() for value in values]
    successes = sum(200 <= status < 300 for status, _ in all_values)
    return LoadReport(
        profile=profile_name,
        concurrency=profile.concurrency,
        synthetic_users=len(keys),
        requests=len(all_values),
        successes=successes,
        expected_rate_limits=rate_limits,
        unexpected_failures=len(all_values) - successes - rate_limits,
        seconds=round(duration, 3),
        requests_per_second=round(len(all_values) / duration, 3),
        latency=latency([seconds for _, seconds in all_values]),
        routes=routes,
        response_bounds_passed=bounds,
        checked_out_connections=checked_out,
    )
