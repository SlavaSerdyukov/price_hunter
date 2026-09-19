from decimal import Decimal

import httpx
import pytest

from pricehunter.api.app import create_app
from pricehunter.core.security import token_digest
from pricehunter.db.models import APIKey

pytestmark = pytest.mark.integration


async def api_client(container, telegram_id):
    user = await container.users.telegram(telegram_id)
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
