"""Remove the unwired backup permission (T-31, AC-20).

Backups are performed by operator-run scripts under deploy/backup with OS-level
access to the database and key material; no API route ever checked
'backup.manage'. The ruling allowed either wiring the permission or removing it,
and removal is the honest option: a permission that guards nothing is a false
assurance in the catalogue.

Grants are deleted before the catalogue row so the downgrade can restore both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_remove_backup_permission"
down_revision = "0021_admin_company_permission"
branch_labels = None
depends_on = None

REMOVED_PERMISSION = ("backup.manage", "backup", "manage", "Run and restore backups")


def upgrade() -> None:
    """Drop the grants and the catalogue row."""
    code = REMOVED_PERMISSION[0]
    op.execute(
        sa.text("DELETE FROM role_permission WHERE permission_code = :code").bindparams(code=code)
    )
    op.execute(sa.text("DELETE FROM permission WHERE code = :code").bindparams(code=code))


def downgrade() -> None:
    """Restore the catalogue row and the tenant-administrator grant."""
    code, module, action, description = REMOVED_PERMISSION
    op.execute(
        sa.text(
            "INSERT INTO permission (code, module, action, description) "
            "VALUES (:code, :module, :action, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(code=code, module=module, action=action, description=description)
    )
    op.execute(
        sa.text(
            "INSERT INTO role_permission (tenant_id, role_id, permission_code) "
            "SELECT r.tenant_id, r.id, :permission_code FROM role r "
            "WHERE r.code = 'tenant_admin' "
            "ON CONFLICT DO NOTHING"
        ).bindparams(permission_code=code)
    )
