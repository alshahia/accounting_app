"""Problem+json error contract."""

from __future__ import annotations

from httpx import AsyncClient


async def test_unknown_route_returns_problem_json(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["code"] == "http_404"
    assert body["title"] == "Not found"
    assert body["request_id"]


async def test_method_not_allowed_returns_problem_json(client: AsyncClient) -> None:
    response = await client.delete("/api/v1/health")

    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "http_405"
