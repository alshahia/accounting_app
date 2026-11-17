"""Version 1 API routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.cashbank import router as cashbank_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.credentials import router as credentials_router
from app.api.v1.health import router as health_router
from app.api.v1.imports import router as imports_router
from app.api.v1.inventory import router as inventory_router
from app.api.v1.ledger import router as ledger_router
from app.api.v1.purchasing import router as purchasing_router
from app.api.v1.reports import router as reports_router
from app.api.v1.sales import router as sales_router
from app.api.v1.settlements import router as settlements_router
from app.api.v1.setup import router as setup_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
api_router.include_router(credentials_router)
api_router.include_router(audit_router)
api_router.include_router(admin_router)
api_router.include_router(ledger_router)
api_router.include_router(catalog_router)
api_router.include_router(cashbank_router)
api_router.include_router(settlements_router)
api_router.include_router(sales_router)
api_router.include_router(purchasing_router)
api_router.include_router(reports_router)
api_router.include_router(imports_router)
api_router.include_router(inventory_router)
