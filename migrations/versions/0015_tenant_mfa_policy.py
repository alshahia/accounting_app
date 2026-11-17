"""tenant MFA enforcement policy

Revision ID: 0015_tenant_mfa_policy
Revises: 0014_tax_jurisdiction
Create Date: 2026-10-22 11:25:00

analysis/21 §6 makes TOTP mandatory for administrators and for users holding
period close/reopen, user/role management or backup permissions, and makes it
a default-on tenant setting for posting users. The policy lives on the tenant
row as data, so a deployment can record an explicit exception without a code
change. Both defaults are on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_tenant_mfa_policy"
down_revision: str | Sequence[str] | None = "0014_tax_jurisdiction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two MFA policy switches to the tenant."""
    op.add_column(
        "tenant",
        sa.Column(
            "mfa_sensitive_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "tenant",
        sa.Column(
            "mfa_posting_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Drop the MFA policy switches."""
    op.drop_column("tenant", "mfa_posting_required")
    op.drop_column("tenant", "mfa_sensitive_required")
