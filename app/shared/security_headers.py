"""Security response headers (report 21 §6, ruling AC-7).

Every response — success, error, authenticated or not — leaves with the same
baseline: no MIME sniffing, no framing, no referrer leakage and a
deny-by-default content policy. HSTS is emitted only when the deployment is
served over HTTPS (production), because browsers ignore it on plain HTTP and a
development header would outlive the development server.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# The generated API documentation is a developer surface (disabled in
# production) that loads its own bundled UI; it needs a usable policy or the
# pages render blank.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self' https: data: 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
)
STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

_DOCS_PREFIXES = ("/docs", "/redoc")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the baseline security headers to every response."""

    def __init__(self, app: object, *, hsts: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type] - ASGI application
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        is_docs = request.url.path in {"/docs", "/redoc", "/openapi.json"} or (
            request.url.path.startswith(_DOCS_PREFIXES)
        )
        headers.setdefault(
            "Content-Security-Policy",
            DOCS_CONTENT_SECURITY_POLICY if is_docs else CONTENT_SECURITY_POLICY,
        )
        if self._hsts:
            headers.setdefault("Strict-Transport-Security", STRICT_TRANSPORT_SECURITY)
        return response
