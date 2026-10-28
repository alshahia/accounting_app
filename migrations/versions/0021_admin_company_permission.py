"""Grant the company-management permission used by the admin API (T-29, AC-10).

The fresh-install admin API creates additional companies, so the permission
catalogue gains 'admin.company.manage'. Existing installations receive the row
and the grant here; new tenants get both from seed_tenant_roles.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_admin_company_permission"
down_revision = "0020_record_integrity"
branch_labels = None
depends_on = None

NEW_PERMISSION = (
    "admin.company.manage",
    "admin",
    "company.manage",
    "Create and configure companies",
)

ROLE_GRANTS = {
    "tenant_admin": {NEW_PERMISSION[0]},
    "company_admin": {NEW_PERMISSION[0]},
}


def _insert_permission() -> None:
    code, module, action, description = NEW_PERMISSION
    op.execute(
        sa.text(
            "INSERT INTO permission (code, module, action, description) "
            "VALUES (:code, :module, :action, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(code=code, module=module, action=action, description=description)
    )
    for role_code, grants in ROLE_GRANTS.items():
        for permission_code in sorted(grants):
            op.execute(
                sa.text(
                    "INSERT INTO role_permission (tenant_id, role_id, permission_code) "
                    "SELECT r.tenant_id, r.id, :permission_code FROM role r "
                    "WHERE r.code = :role_code "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role_code=role_code, permission_code=permission_code)
            )


def upgrade() -> None:
    """Add the permission row and grant it to the two administrator roles."""
    _insert_permission()


def downgrade() -> None:
    """Remove the grants and the permission row."""
    code = NEW_PERMISSION[0]
    op.execute(
        sa.text("DELETE FROM role_permission WHERE permission_code = :code").bindparams(code=code)
    )
    op.execute(sa.text("DELETE FROM permission WHERE code = :code").bindparams(code=code))
