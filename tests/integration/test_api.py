from decimal import Decimal

import httpx
import pytest

from pricehunter.api.app import create_app
from pricehunter.core.security import token_digest
from pricehunter.db.models import APIKey
from pricehunter.localization.languages import SUPPORTED_LANGUAGES
from tests.support import grant_plan

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_api_language_preference_persists(container, language):
    client = await api_client(container, 111)
    async with client:
        response = await client.patch("/api/v1/users/me/settings", json={"language_code": language})
        assert response.status_code == 200 and response.json()["language_code"] == language
        assert (await container.users.telegram(111)).language_code == language
        assert await container.users.language(111) == language
        response = await client.patch("/api/v1/users/me/settings", json={"language_code": "nl"})
        assert response.status_code == 422
        assert await container.users.language(111) == language


async def api_client(container, telegram_id, *, country="BE"):
    user = await container.users.telegram(telegram_id)
    if country is not None:
        from pricehunter.schemas.api import UserSettingsPatch

        await container.users.settings(user.id, UserSettingsPatch(country_code=country))
    token = f"ph_test_only_known_key_{telegram_id}_123456789"
    async with container.sessions.begin() as session:
        session.add(APIKey(user_id=user.id, digest=token_digest(token), label="test"))
    app = create_app(container.settings, container)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    return client


async def test_api_flow_authentication_ownership_and_history(container):
    client = await api_client(container, 111)
    other = await api_client(container, 222)
    async with client, other:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        assert (
            await client.get("/api/v1/trackers", headers={"Authorization": ""})
        ).status_code == 401
        response = await client.post(
            "/api/v1/products/resolve",
            json={
                "url": "https://mock.pricehunter.test/products/headphones",
            },
        )
        assert response.status_code == 200, response.text
        offer = response.json()
        assert Decimal(offer["price"]) == 100
        response = await client.post("/api/v1/trackers", json={"store_offer_id": offer["id"]})
        assert response.status_code == 201, response.text
        tracker = response.json()
        assert (
            await other.patch(f"/api/v1/trackers/{tracker['id']}", json={"enabled": False})
        ).status_code == 404
        await grant_plan(container, (await container.users.telegram(111)).id)
        response = await client.patch(
            f"/api/v1/trackers/{tracker['id']}", json={"target_price": "90.50"}
        )
        assert response.status_code == 200
        assert Decimal(response.json()["target_price"]) == Decimal("90.50")
        response = await client.get(
            f"/api/v1/products/{offer['product_id']}/history", params={"offer_id": offer["id"]}
        )
        assert response.status_code == 200 and response.json()["count"] == 1
        assert (
            await client.patch(f"/api/v1/trackers/{tracker['id']}", json={"enabled": None})
        ).status_code == 422
        assert (await client.get("/api/v1/search", params={"q": "Headphones"})).status_code == 200
        assert (await client.get("/api/v1/subscriptions/me")).json()["checkout_available"] is False
        assert (await client.delete(f"/api/v1/trackers/{tracker['id']}")).status_code == 204
        assert (await client.get("/api/v1/trackers")).json() == []


async def test_api_limits_ssrf_body_and_webhook_auth(container):
    client = await api_client(container, 111)
    async with client:
        response = await client.post(
            "/api/v1/products/resolve", json={"url": "https://127.0.0.1/private"}
        )
        assert response.status_code == 400 and response.json()["error"] == "invalid_url"
        response = await client.post("/api/v1/products/resolve", content=b"x" * 70000)
        assert response.status_code == 413
        assert (await client.post("/telegram/webhook", json={"update_id": 1})).status_code == 403
        assert (
            await client.get("/health/live", headers={"Host": "evil.example"})
        ).status_code == 400
        container.settings.user_requests_per_minute = 1
        response = await client.get("/api/v1/search", params={"q": "headphones"})
        assert response.status_code == 429 and response.headers["Retry-After"] == "60"


async def test_api_reports_effective_subscription_features_and_expiry(container, monkeypatch):
    from datetime import timedelta

    from pricehunter.db.base import utcnow

    client = await api_client(container, 333)
    user = await container.users.telegram(333)
    until = utcnow() + timedelta(days=1)
    await grant_plan(container, user.id, until=until)
    async with client:
        current = (await client.get("/api/v1/subscriptions/me")).json()
        assert current["plan"] == "pro" and current["features"]["target_price_alerts"]
        assert current["tracker_limit"] == 50 and current["tracker_count"] == 0
        monkeypatch.setattr(
            "pricehunter.services.entitlement_service.utcnow", lambda: until + timedelta(seconds=1)
        )
        expired = (await client.get("/api/v1/subscriptions/me")).json()
        assert expired["plan"] == "free" and expired["status"] == "expired"
        assert expired["tracker_limit"] == 2 and not expired["features"]["target_price_alerts"]
