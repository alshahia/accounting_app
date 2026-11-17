"""Seed the permission catalogue and default role bundles.

The catalogue itself is seeded by migration 0003 so every database has it; this
module keeps a tenant's default roles in sync and is idempotent. Tenant creation
(admin tooling, day 4+) calls `seed_tenant_roles`.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import Permission, Role, RolePermission
from app.modules.identity.permissions import DEFAULT_ROLES, PERMISSION_DESCRIPTIONS


async def seed_permissions(session: AsyncSession) -> None:
    """Insert any catalogue rows that are missing."""
    existing = set((await session.execute(select(Permission.code))).scalars().all())
    for code, description in PERMISSION_DESCRIPTIONS.items():
        if code in existing:
            continue
        module, action = code.split(".", 1)
        session.add(Permission(code=code, module=module, action=action, description=description))
    await session.flush()


async def seed_tenant_roles(session: AsyncSession, tenant_id: UUID) -> dict[str, Role]:
    """Create the default roles for one tenant if they are missing; return them."""
    existing = {
        role.code: role
        for role in (
            await session.execute(select(Role).where(Role.tenant_id == tenant_id))
        ).scalars()
    }
    roles: dict[str, Role] = {}
    for code, definition in DEFAULT_ROLES.items():
        role = existing.get(code)
        if role is None:
            role = Role(tenant_id=tenant_id, code=code, name=definition.name, is_system=True)
            session.add(role)
            await session.flush()
        roles[code] = role

        assigned = set(
            (
                await session.execute(
                    select(RolePermission.permission_code).where(RolePermission.role_id == role.id)
                )
            )
            .scalars()
            .all()
        )
        for permission_code in sorted(definition.permissions - assigned):
            session.add(
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=role.id,
                    permission_code=permission_code,
                )
            )
    await session.flush()
    return roles
