"""authentication and memberships

Revision ID: 0003_auth_memberships
Revises: 0002_identity_tenancy
Create Date: 2026-10-21 13:00:00

Adds the day-3 identity surface: company memberships with roles, the global
permission catalogue plus tenant-owned role bundles, refresh-token sessions and
the audit log. `app_user` gains the lockout counters. Every tenant-scoped table
gets the same fail-closed RLS policy as day 2.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_auth_memberships"
down_revision: str | Sequence[str] | None = "0002_identity_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = (
    "membership",
    "role",
    "role_permission",
    "membership_role",
    "auth_session",
    "audit_log",
)

PERMISSIONS: tuple[tuple[str, str, str, str], ...] = (
    ("admin.user.manage", "admin", "user.manage", "Create, update and deactivate users"),
    ("admin.role.manage", "admin", "role.manage", "Create roles and assign permissions"),
    ("ledger.journal.create", "ledger", "journal.create", "Create draft journal entries"),
    ("ledger.journal.post", "ledger", "journal.post", "Post journal entries"),
    ("ledger.period.close", "ledger", "period.close", "Close and reopen accounting periods"),
    ("sales.invoice.create", "sales", "invoice.create", "Create sales invoices"),
    ("sales.invoice.post", "sales", "invoice.post", "Post sales invoices"),
    ("purchasing.bill.create", "purchasing", "bill.create", "Create purchase bills"),
    ("purchasing.bill.post", "purchasing", "bill.post", "Post purchase bills"),
    ("inventory.manage", "inventory", "manage", "Manage items, warehouses and stock moves"),
    ("cashbank.manage", "cashbank", "manage", "Manage cash and bank accounts and payments"),
    ("tax.manage", "tax", "manage", "Manage tax codes and tax reports"),
    ("report.view", "report", "view", "Read financial and operational reports"),
    ("audit.view", "audit", "view", "Read the audit log"),
    ("settings.manage", "settings", "manage", "Manage company settings and numbering"),
    ("backup.manage", "backup", "manage", "Run and restore backups"),
)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    ]


def _actors() -> list[sa.Column[object]]:
    return [
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
    ]


def _actor_constraints(table: str) -> list[sa.schema.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=f"fk_{table}_created_by_app_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name=f"fk_{table}_updated_by_app_user",
            ondelete="SET NULL",
        ),
    ]


def _extend_app_user() -> None:
    op.add_column(
        "app_user",
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("app_user", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_app_user_tenant_id", "app_user", ["tenant_id", "id"])


def _create_membership() -> None:
    op.create_table(
        "membership",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("app_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_membership"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "app_user_id"],
            ["app_user.tenant_id", "app_user.id"],
            name="fk_membership_tenant_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_membership_tenant_id_company",
            ondelete="CASCADE",
        ),
        *_actor_constraints("membership"),
        sa.UniqueConstraint("app_user_id", "company_id", name="uq_membership_app_user_company"),
    )
    op.create_index("ix_membership_tenant_id", "membership", ["tenant_id"])
    op.create_index("ix_membership_app_user_id", "membership", ["app_user_id"])
    op.create_index("ix_membership_company_id", "membership", ["company_id"])


def _create_roles() -> None:
    op.create_table(
        "role",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_role"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_role_tenant_id_tenant", ondelete="CASCADE"
        ),
        *_actor_constraints("role"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),
    )
    op.create_index("ix_role_tenant_id", "role", ["tenant_id"])

    op.create_table(
        "permission",
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("module", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("code", name="pk_permission"),
    )

    op.create_table(
        "role_permission",
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("permission_code", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission_code", name="pk_role_permission"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_role_permission_tenant_id_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["role.id"], name="fk_role_permission_role_id_role", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["permission_code"],
            ["permission.code"],
            name="fk_role_permission_permission_code_permission",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_role_permission_tenant_id", "role_permission", ["tenant_id"])

    op.create_table(
        "membership_role",
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("membership_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("membership_id", "role_id", name="pk_membership_role"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_membership_role_tenant_id_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"],
            ["membership.id"],
            name="fk_membership_role_membership_id_membership",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["role.id"], name="fk_membership_role_role_id_role", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_membership_role_tenant_id", "membership_role", ["tenant_id"])


def _create_sessions_and_audit() -> None:
    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("app_user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("family_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_session_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_auth_session"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_auth_session_tenant_id_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "app_user_id"],
            ["app_user.tenant_id", "app_user.id"],
            name="fk_auth_session_tenant_id_app_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_session_id"],
            ["auth_session.id"],
            name="fk_auth_session_replaced_by_session_id_auth_session",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),
    )
    op.create_index("ix_auth_session_tenant_id", "auth_session", ["tenant_id"])
    op.create_index("ix_auth_session_app_user_id", "auth_session", ["app_user_id"])
    op.create_index("ix_auth_session_family_id", "auth_session", ["family_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_audit_log_tenant_id_tenant", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["app_user.id"],
            name="fk_audit_log_actor_user_id_app_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])


def _enable_rls() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} FOR ALL TO PUBLIC "
            f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
        )


def _grant_and_seed() -> None:
    op.execute("GRANT SELECT ON TABLE public.permission TO ameen_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "public.membership, public.role, public.role_permission, public.membership_role, "
        "public.auth_session, public.audit_log TO ameen_app"
    )
    for code, module, action, description in PERMISSIONS:
        op.execute(
            sa.text(
                "INSERT INTO permission (code, module, action, description) "
                "VALUES (:code, :module, :action, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(
                code=code,
                module=module,
                action=action,
                description=description,
            )
        )


def upgrade() -> None:
    """Extend app_user, then add memberships, roles, sessions and the audit log."""
    _extend_app_user()
    _create_membership()
    _create_roles()
    _create_sessions_and_audit()
    _enable_rls()
    _grant_and_seed()


def downgrade() -> None:
    """Revert the schema change."""
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_auth_session_family_id", table_name="auth_session")
    op.drop_index("ix_auth_session_app_user_id", table_name="auth_session")
    op.drop_index("ix_auth_session_tenant_id", table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_index("ix_membership_role_tenant_id", table_name="membership_role")
    op.drop_table("membership_role")
    op.drop_index("ix_role_permission_tenant_id", table_name="role_permission")
    op.drop_table("role_permission")
    op.drop_table("permission")
    op.drop_index("ix_role_tenant_id", table_name="role")
    op.drop_table("role")
    op.drop_index("ix_membership_company_id", table_name="membership")
    op.drop_index("ix_membership_app_user_id", table_name="membership")
    op.drop_index("ix_membership_tenant_id", table_name="membership")
    op.drop_table("membership")
    op.drop_constraint("uq_app_user_tenant_id", "app_user", type_="unique")
    op.drop_column("app_user", "locked_until")
    op.drop_column("app_user", "failed_login_count")
