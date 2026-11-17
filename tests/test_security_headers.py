"""Baseline security headers (ruling AC-7)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.shared.security_headers import SecurityHeadersMiddleware


async def _endpoint(request: Request) -> Response:
    return PlainTextResponse("ok")


def _app(*, hsts: bool) -> SecurityHeadersMiddleware:
    routes: list[Route] = [Route("/ping", _endpoint), Route("/docs", _endpoint)]
    return SecurityHeadersMiddleware(Starlette(routes=routes), hsts=hsts)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_app(hsts=False))
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def _get(client: AsyncClient, path: str) -> Response:
    return await client.get(path)


async def test_baseline_headers_are_present(client: AsyncClient) -> None:
    response = await _get(client, "/ping")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "strict-transport-security" not in response.headers


async def test_hsts_is_emitted_only_when_configured(client: AsyncClient) -> None:
    transport = ASGITransport(app=_app(hsts=True))
    async with AsyncClient(transport=transport, base_url="http://testserver") as https_client:
        response = await https_client.get("/ping")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


async def test_api_documentation_gets_a_usable_policy(client: AsyncClient) -> None:
    response = await _get(client, "/docs")

    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
