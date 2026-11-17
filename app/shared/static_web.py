"""Serve the built single-page frontend from the API process (T-31, AC-14).

The production image ships one artifact: FastAPI serves the compiled React bundle
from the same origin as the API. Same-origin delivery keeps the session cookie,
the CSRF cookie and the Content-Security-Policy coherent, and it removes the
reverse-proxy rewrite rules an operator would otherwise have to get right.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

DEFAULT_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def resolve_web_dist() -> Path | None:
    """The directory that holds index.html and assets/, or None."""
    candidate = Path(os.environ.get("MAHMOUD_WEB_DIST", str(DEFAULT_DIST)))
    if (candidate / "index.html").is_file():
        return candidate
    return None


def mount_web(application: FastAPI) -> bool:
    """Mount the bundle at the root, leaving API and docs routes untouched.

    Returns whether a bundle was found. The catch-all is registered last, so
    every route added earlier still wins; unmatched /api paths keep their JSON
    404 instead of being swallowed by the SPA fallback.
    """
    dist = resolve_web_dist()
    if dist is None:
        return False
    assets = dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @application.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (dist / path).resolve()
        if path != "" and candidate.is_file() and dist in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-store"})

    return True
