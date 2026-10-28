"""posting sequences, idempotency and reversal links

Revision ID: 0005_posting
Revises: 0004_ledger
Create Date: 2026-10-21 13:20:00

Day 6: the posting service needs two small tables and one unique index. A
number-sequence row hands out journal-entry numbers per company and period key
and serializes concurrent posts by row lock; an idempotency record maps a
client key to the entry its first call created so a retry returns the same
result; a partial unique index guarantees that one entry can be reversed at
most once. Both tables are tenant-scoped with the same fail-closed RLS policy as
the ledger tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_posting"
down_revision: str | Sequence[str] | None = "0004_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = ("number_sequence", "idempotency_record")


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
    ]


def _create_number_sequence() -> None:
    op.create_table(
        "number_sequence",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("period_key", sa.String(length=20), nullable=False),
        sa.Column("next_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_number_sequence"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_number_sequence_tenant_company",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "scope",
            "period_key",
            name="uq_number_sequence_tenant_company_scope_period",
        ),
        sa.CheckConstraint("next_value >= 0", name="ck_number_sequence_next_value_non_negative"),
    )
    op.create_index("ix_number_sequence_tenant_id", "number_sequence", ["tenant_id"])
    op.create_index("ix_number_sequence_company_id", "number_sequence", ["company_id"])
    op.create_index(
        "ix_number_sequence_tenant_company", "number_sequence", ["tenant_id", "company_id"]
    )


def _create_idempotency_record() -> None:
    op.create_table(
        "idempotency_record",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'in_progress'"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_record"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_idempotency_record_tenant_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_idempotency_record_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "key",
            name="uq_idempotency_record_tenant_company_key",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_idempotency_record_status_allowed",
        ),
    )
    op.create_index("ix_idempotency_record_tenant_id", "idempotency_record", ["tenant_id"])
    op.create_index("ix_idempotency_record_company_id", "idempotency_record", ["company_id"])
    op.create_index(
        "ix_idempotency_record_tenant_company",
        "idempotency_record",
        ["tenant_id", "company_id"],
    )


def _create_reversal_index() -> None:
    op.create_index(
        "uq_journal_entry_reversal",
        "journal_entry",
        ["tenant_id", "company_id", "reverses_entry_id"],
        unique=True,
        postgresql_where=sa.text("reverses_entry_id IS NOT NULL"),
    )


def _enable_rls_and_grant() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} FOR ALL TO PUBLIC "
            f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
        )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "public.number_sequence, public.idempotency_record TO ameen_app"
    )


def upgrade() -> None:
    """Create the posting-support tables and the reversal uniqueness index."""
    _create_number_sequence()
    _create_idempotency_record()
    _create_reversal_index()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the reversal index and the posting-support tables."""
    op.drop_index("uq_journal_entry_reversal", table_name="journal_entry")
    for index in (
        "ix_idempotency_record_tenant_company",
        "ix_idempotency_record_company_id",
        "ix_idempotency_record_tenant_id",
    ):
        op.drop_index(index, table_name="idempotency_record")
    op.drop_table("idempotency_record")
    for index in (
        "ix_number_sequence_tenant_company",
        "ix_number_sequence_company_id",
        "ix_number_sequence_tenant_id",
    ):
        op.drop_index(index, table_name="number_sequence")
    op.drop_table("number_sequence")
