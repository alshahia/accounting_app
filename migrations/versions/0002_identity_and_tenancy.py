"""identity and tenancy

Revision ID: 0002_identity_tenancy
Revises: 0001_baseline
Create Date: 2026-10-21 12:45:00

Adds the tenant root and the tables the rest of the schema hangs off:

* `tenant`, `company`, `branch` and `app_user` with UUIDv7 keys, audit
  columns and the naming conventions from report 21 §4.1;
* ENABLE + FORCE ROW LEVEL SECURITY with a fail-closed tenant policy on every
  table (a transaction without `app.tenant_id` sees nothing);
* the `ameen_app` role (NOLOGIN, NOSUPERUSER, NOBYPASSRLS) and the grants the
  application uses.

The tenant row predates every user, so its actor columns are plain ids without a
foreign key. The application role is created without a password; a deployment
grants it LOGIN and a password out of band, never from the repository.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_identity_tenancy"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IDENTITY_TABLES = ("app_user", "company", "branch")
RLS_TABLES = ("tenant", *IDENTITY_TABLES)
TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
SELF_EXPRESSION = "id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


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


def _create_identity_tables() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'active'"), nullable=False
        ),
        *_timestamps(),
        # No FK: the first user is created after the tenant exists.
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tenant"),
        sa.UniqueConstraint("slug", name="uq_tenant_slug"),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_tenant_status_allowed"),
    )

    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_app_user_tenant_id_tenant", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_app_user_created_by_app_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_app_user_updated_by_app_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])

    op.create_table(
        "company",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("base_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("tax_number", sa.String(length=50), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'active'"), nullable=False
        ),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_company"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_company_tenant_id_tenant", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_company_created_by_app_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_company_updated_by_app_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "code", name="uq_company_tenant_code"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_company_tenant_id"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_company_status_allowed"),
        sa.CheckConstraint("char_length(base_currency) = 3", name="ck_company_base_currency_iso"),
    )
    op.create_index("ix_company_tenant_id", "company", ["tenant_id"])

    op.create_table(
        "branch",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_branch"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_branch_tenant_id_tenant", ondelete="CASCADE"
        ),
        # Composite reference: a branch can never point at another tenant's company.
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_branch_tenant_id_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name="fk_branch_created_by_app_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["app_user.id"],
            name="fk_branch_updated_by_app_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_branch_tenant_company_code"
        ),
    )
    op.create_index("ix_branch_tenant_id", "branch", ["tenant_id"])
    op.create_index("ix_branch_company_id", "branch", ["company_id"])


def _enable_rls() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"CREATE POLICY tenant_isolation ON public.tenant FOR ALL TO PUBLIC "
        f"USING ({SELF_EXPRESSION}) WITH CHECK ({SELF_EXPRESSION})"
    )
    for table in IDENTITY_TABLES:
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} FOR ALL TO PUBLIC "
            f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
        )


def _create_application_role() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameen_app') THEN
                EXECUTE 'CREATE ROLE ameen_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO ameen_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "public.tenant, public.app_user, public.company, public.branch TO ameen_app"
    )
    op.execute(
        """
        DO $$
        DECLARE
            owner_name name := current_user;
        BEGIN
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ameen_app',
                owner_name
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT USAGE, SELECT ON SEQUENCES TO ameen_app',
                owner_name
            );
        END
        $$;
        """
    )


def upgrade() -> None:
    """Create the identity tables, their RLS policies and the application role."""
    _create_identity_tables()
    _enable_rls()
    _create_application_role()


def downgrade() -> None:
    """Revert the schema change.

    The cluster-wide `ameen_app` role is intentionally left in place: dropping a
    role can fail while it holds grants in another database of the same cluster.
    """
    op.drop_index("ix_branch_company_id", table_name="branch")
    op.drop_index("ix_branch_tenant_id", table_name="branch")
    op.drop_table("branch")
    op.drop_index("ix_company_tenant_id", table_name="company")
    op.drop_table("company")
    op.drop_index("ix_app_user_tenant_id", table_name="app_user")
    op.drop_table("app_user")
    op.drop_table("tenant")
