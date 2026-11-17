"""ledger schema and invariants

Revision ID: 0004_ledger
Revises: 0003_auth_memberships
Create Date: 2026-10-21 13:10:00

Day 5: the chart of accounts, fiscal years/periods, journal entries and lines,
and document links. The database enforces the accounting invariants directly:
a deferred constraint trigger requires debit = credit at commit and at least two
lines, posted entries are immutable, only postable active accounts of the
entry's own company can be used, a closed period rejects postings, and a source
document can produce only one entry. Every table is tenant-scoped with the same
fail-closed RLS policy as the identity tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ledger"
down_revision: str | Sequence[str] | None = "0003_auth_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = (
    "account",
    "fiscal_year",
    "accounting_period",
    "journal_entry",
    "journal_line",
    "journal_entry_document",
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


def _create_account() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("account_type", sa.String(length=20), nullable=False),
        sa.Column("is_postable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_account"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_account_tenant_company",
            ondelete="CASCADE",
        ),
        *_actor_constraints("account"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_account_tenant_company_code"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_account_tenant_company_id"),
        sa.CheckConstraint(
            "account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')",
            name="ck_account_type_allowed",
        ),
    )
    op.create_index("ix_account_tenant_id", "account", ["tenant_id"])
    op.create_index("ix_account_company_id", "account", ["company_id"])
    op.create_index("ix_account_tenant_company", "account", ["tenant_id", "company_id"])


def _create_fiscal_year() -> None:
    op.create_table(
        "fiscal_year",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name_ar", sa.String(length=100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_fiscal_year"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_fiscal_year_tenant_company",
            ondelete="CASCADE",
        ),
        *_actor_constraints("fiscal_year"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_fiscal_year_tenant_company_code"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_fiscal_year_tenant_company_id"
        ),
        sa.CheckConstraint("end_date > start_date", name="ck_fiscal_year_dates_ordered"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_fiscal_year_status_allowed"),
    )
    op.create_index("ix_fiscal_year_tenant_id", "fiscal_year", ["tenant_id"])
    op.create_index("ix_fiscal_year_company_id", "fiscal_year", ["company_id"])
    op.create_index("ix_fiscal_year_tenant_company", "fiscal_year", ["tenant_id", "company_id"])


def _create_period() -> None:
    op.create_table(
        "accounting_period",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("fiscal_year_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_accounting_period"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "fiscal_year_id"],
            ["fiscal_year.tenant_id", "fiscal_year.company_id", "fiscal_year.id"],
            name="fk_accounting_period_fiscal_year",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"],
            ["app_user.id"],
            name="fk_accounting_period_closed_by_app_user",
            ondelete="SET NULL",
        ),
        *_actor_constraints("accounting_period"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "fiscal_year_id",
            "number",
            name="uq_accounting_period_tenant_company_number",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_accounting_period_tenant_company_id"
        ),
        sa.CheckConstraint("end_date >= start_date", name="ck_accounting_period_dates_ordered"),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name="ck_accounting_period_status_allowed"
        ),
    )
    op.create_index("ix_accounting_period_tenant_id", "accounting_period", ["tenant_id"])
    op.create_index("ix_accounting_period_company_id", "accounting_period", ["company_id"])
    op.create_index(
        "ix_accounting_period_tenant_company", "accounting_period", ["tenant_id", "company_id"]
    )


def _create_entry() -> None:
    op.create_table(
        "journal_entry",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("period_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=30), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("source_type", sa.String(length=50), nullable=True),
        sa.Column("source_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("reverses_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_actors(),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entry"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "period_id"],
            ["accounting_period.tenant_id", "accounting_period.company_id", "accounting_period.id"],
            name="fk_journal_entry_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "reverses_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_entry_reverses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"],
            ["app_user.id"],
            name="fk_journal_entry_posted_by_app_user",
            ondelete="SET NULL",
        ),
        *_actor_constraints("journal_entry"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_journal_entry_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_journal_entry_tenant_company_id"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_journal_entry_status_allowed"),
    )
    op.create_index("ix_journal_entry_tenant_id", "journal_entry", ["tenant_id"])
    op.create_index("ix_journal_entry_company_id", "journal_entry", ["company_id"])
    op.create_index(
        "ix_journal_entry_tenant_company_date",
        "journal_entry",
        ["tenant_id", "company_id", "entry_date"],
    )
    op.create_index(
        "ix_journal_entry_tenant_company_source",
        "journal_entry",
        ["tenant_id", "company_id", "source_type"],
    )
    op.create_index(
        "uq_journal_entry_source",
        "journal_entry",
        ["tenant_id", "company_id", "source_type", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_type IS NOT NULL"),
    )


def _create_line_and_document() -> None:
    op.create_table(
        "journal_line",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entry_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("debit_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("credit_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_journal_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_line_entry",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_journal_line_account",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "entry_id",
            "line_no",
            name="uq_journal_line_tenant_company_entry_line",
        ),
        sa.CheckConstraint("debit_minor >= 0", name="ck_journal_line_debit_non_negative"),
        sa.CheckConstraint("credit_minor >= 0", name="ck_journal_line_credit_non_negative"),
        sa.CheckConstraint(
            "(debit_minor > 0) <> (credit_minor > 0)",
            name="ck_journal_line_one_side_positive",
        ),
    )
    op.create_index("ix_journal_line_tenant_id", "journal_line", ["tenant_id"])
    op.create_index("ix_journal_line_company_id", "journal_line", ["company_id"])
    op.create_index(
        "ix_journal_line_tenant_company_entry",
        "journal_line",
        ["tenant_id", "company_id", "entry_id"],
    )

    op.create_table(
        "journal_entry_document",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("entry_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_journal_entry_document"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_entry_document_entry",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "entry_id",
            "document_type",
            "document_id",
            name="uq_journal_entry_document_link",
        ),
    )
    op.create_index("ix_journal_entry_document_tenant_id", "journal_entry_document", ["tenant_id"])
    op.create_index(
        "ix_journal_entry_document_company_id", "journal_entry_document", ["company_id"]
    )
    op.create_index(
        "ix_journal_entry_document_tenant_company",
        "journal_entry_document",
        ["tenant_id", "company_id"],
    )


def _create_invariant_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.journal_entry_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'posted' THEN
                RAISE EXCEPTION 'posted journal entry % is immutable', OLD.number
                    USING ERRCODE = 'check_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.journal_entry_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            period_status text;
            period_start date;
            period_end date;
        BEGIN
            SELECT status, start_date, end_date
              INTO period_status, period_start, period_end
              FROM accounting_period
             WHERE tenant_id = NEW.tenant_id
               AND company_id = NEW.company_id
               AND id = NEW.period_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'period % does not belong to this company', NEW.period_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.entry_date < period_start OR NEW.entry_date > period_end THEN
                RAISE EXCEPTION 'entry date % is outside the selected period', NEW.entry_date
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status = 'posted' THEN
                IF period_status <> 'open' THEN
                    RAISE EXCEPTION 'period % is closed', NEW.period_id
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.posted_at IS NULL OR NEW.posted_by IS NULL THEN
                    RAISE EXCEPTION 'a posted entry needs posted_at and posted_by'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.journal_line_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM account a
                 WHERE a.id = NEW.account_id
                   AND a.tenant_id = NEW.tenant_id
                   AND a.company_id = NEW.company_id
                   AND a.is_postable
                   AND a.is_active
            ) THEN
                RAISE EXCEPTION 'account % is not a postable active account of this company',
                    NEW.account_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.check_journal_balance()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_entry uuid;
            v_debit bigint;
            v_credit bigint;
            v_lines bigint;
        BEGIN
            v_entry := COALESCE(NEW.entry_id, OLD.entry_id);
            IF NOT EXISTS (SELECT 1 FROM journal_entry WHERE id = v_entry) THEN
                RETURN NULL;
            END IF;
            SELECT COALESCE(SUM(debit_minor), 0),
                   COALESCE(SUM(credit_minor), 0),
                   COUNT(*)
              INTO v_debit, v_credit, v_lines
              FROM journal_line
             WHERE entry_id = v_entry;
            IF v_lines < 2 THEN
                RAISE EXCEPTION 'journal entry % must have at least two lines', v_entry
                    USING ERRCODE = 'check_violation';
            END IF;
            IF v_debit <> v_credit THEN
                RAISE EXCEPTION 'journal entry % is unbalanced: debit % <> credit %',
                    v_entry, v_debit, v_credit
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.check_posted_entry()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_debit bigint;
            v_credit bigint;
            v_lines bigint;
        BEGIN
            IF NEW.status <> 'posted' THEN
                RETURN NULL;
            END IF;
            SELECT COALESCE(SUM(debit_minor), 0),
                   COALESCE(SUM(credit_minor), 0),
                   COUNT(*)
              INTO v_debit, v_credit, v_lines
              FROM journal_line
             WHERE entry_id = NEW.id;
            IF v_lines < 2 THEN
                RAISE EXCEPTION 'posted entry % must have at least two lines', NEW.number
                    USING ERRCODE = 'check_violation';
            END IF;
            IF v_debit <> v_credit THEN
                RAISE EXCEPTION 'posted entry % is unbalanced: debit % <> credit %',
                    NEW.number, v_debit, v_credit
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )


def _create_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_journal_entry_immutable
        BEFORE UPDATE OR DELETE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION public.journal_entry_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journal_entry_guard
        BEFORE INSERT OR UPDATE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION public.journal_entry_guard()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journal_line_guard
        BEFORE INSERT OR UPDATE ON journal_line
        FOR EACH ROW EXECUTE FUNCTION public.journal_line_guard()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_journal_line_balanced
        AFTER INSERT OR UPDATE OR DELETE ON journal_line
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.check_journal_balance()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_journal_entry_posted
        AFTER INSERT OR UPDATE ON journal_entry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.check_posted_entry()
        """
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
        "public.account, public.fiscal_year, public.accounting_period, "
        "public.journal_entry, public.journal_line, public.journal_entry_document "
        "TO ameen_app"
    )


def upgrade() -> None:
    """Create the ledger tables, then the invariant triggers and RLS policies."""
    _create_account()
    _create_fiscal_year()
    _create_period()
    _create_entry()
    _create_line_and_document()
    _create_invariant_functions()
    _create_triggers()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the triggers, functions and ledger tables."""
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entry_posted ON journal_entry")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_line_balanced ON journal_line")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_line_guard ON journal_line")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entry_guard ON journal_entry")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entry_immutable ON journal_entry")
    for function in (
        "check_posted_entry",
        "check_journal_balance",
        "journal_line_guard",
        "journal_entry_guard",
        "journal_entry_immutable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{function}()")
    for index, table in (
        ("ix_journal_entry_document_tenant_company", "journal_entry_document"),
        ("ix_journal_entry_document_company_id", "journal_entry_document"),
        ("ix_journal_entry_document_tenant_id", "journal_entry_document"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("journal_entry_document")
    for index, table in (
        ("ix_journal_line_tenant_company_entry", "journal_line"),
        ("ix_journal_line_company_id", "journal_line"),
        ("ix_journal_line_tenant_id", "journal_line"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("journal_line")
    for index in (
        "uq_journal_entry_source",
        "ix_journal_entry_tenant_company_source",
        "ix_journal_entry_tenant_company_date",
        "ix_journal_entry_company_id",
        "ix_journal_entry_tenant_id",
    ):
        op.drop_index(index, table_name="journal_entry")
    op.drop_table("journal_entry")
    for index, table in (
        ("ix_accounting_period_tenant_company", "accounting_period"),
        ("ix_accounting_period_company_id", "accounting_period"),
        ("ix_accounting_period_tenant_id", "accounting_period"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("accounting_period")
    for index, table in (
        ("ix_fiscal_year_tenant_company", "fiscal_year"),
        ("ix_fiscal_year_company_id", "fiscal_year"),
        ("ix_fiscal_year_tenant_id", "fiscal_year"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("fiscal_year")
    for index, table in (
        ("ix_account_tenant_company", "account"),
        ("ix_account_company_id", "account"),
        ("ix_account_tenant_id", "account"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("account")
