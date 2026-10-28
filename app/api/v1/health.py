"""Health and readiness probes (unauthenticated)."""

from __future__ import annotations

import logging
from typing import Literal, cast

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.config import Settings
from app.shared.db import Database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness payload."""

    status: Literal["ok"] = "ok"
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """Readiness payload; a degraded instance reports 503."""

    status: Literal["ready", "degraded"]
    database: Literal["ok", "unavailable"]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(request: Request) -> HealthResponse:
    """Return ok when the process is up; it deliberately touches no dependency."""
    settings = cast(Settings, request.app.state.settings)
    return HealthResponse(version=settings.version, environment=settings.environment)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe (checks PostgreSQL)",
    responses={503: {"description": "A dependency is unavailable"}},
)
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    """Return ready only when PostgreSQL answers a trivial query."""
    database = cast(Database, request.app.state.database)
    try:
        await database.ping()
    except Exception:
        logger.warning("readiness check failed: database unavailable", exc_info=True)
        response.status_code = 503
        return ReadinessResponse(status="degraded", database="unavailable")
    return ReadinessResponse(status="ready", database="ok")
