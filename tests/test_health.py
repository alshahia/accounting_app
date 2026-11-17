"""Liveness endpoint and request-id behaviour."""

from __future__ import annotations

from httpx import AsyncClient

from app.config import Settings


async def test_health_returns_ok(client: AsyncClient, settings: Settings) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "version": settings.version, "environment": "test"}


async def test_health_echoes_client_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-Id": "review-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "review-123"


async def test_health_generates_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers["x-request-id"]
