"""MFA recovery codes

Revision ID: 0016_mfa_recovery_codes
Revises: 0015_tenant_mfa_policy
Create Date: 2026-10-22 11:50:00

analysis/21 §6 asks for hashed recovery codes alongside TOTP. Each code is
stored only as an Argon2id hash and is consumed once; regenerating replaces the
whole set. The tenant isolation policy matches every other tenant-owned table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_mfa_recovery_codes"
down_revision: str | Sequence[str] | None = "0015_tenant_mfa_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create the recovery-code table with forced RLS and grants."""
    op.create_table(
        "mfa_recovery_code",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_mfa_recovery_code"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "code_hash",
            name="uq_mfa_recovery_code_tenant_user_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["app_user.tenant_id", "app_user.id"],
            name="fk_mfa_recovery_code_tenant_user",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_mfa_recovery_code_tenant_id", "mfa_recovery_code", ["tenant_id"])
    op.create_index("ix_mfa_recovery_code_user_id", "mfa_recovery_code", ["user_id"])
    op.create_index(
        "ix_mfa_recovery_code_tenant_user",
        "mfa_recovery_code",
        ["tenant_id", "user_id"],
    )
    op.execute("ALTER TABLE public.mfa_recovery_code ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.mfa_recovery_code FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.mfa_recovery_code FOR ALL TO PUBLIC "
        f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.mfa_recovery_code TO ameen_app"
    )


def downgrade() -> None:
    """Drop the recovery-code table."""
    op.drop_table("mfa_recovery_code")
