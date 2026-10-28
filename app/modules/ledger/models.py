"""Ledger models (phase 0, day 5): chart, periods, entries and lines.

The database, not the service layer, owns the invariants: debit = credit at
commit (deferred constraint trigger), posted entries are immutable, only
postable accounts of the entry's own company can be used, a closed period
rejects postings, and a source document can produce at most one journal entry.
The ORM mirrors those constraints so a violation fails loudly either way.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import (
    ActorMixin,
    Base,
    RowVersionMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

ACCOUNT_TYPE_CHECK = "account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')"


class Account(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """One account in a company's chart; only postable leaves take lines."""

    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "code", name="uq_account_tenant_company_code"),
        # Composite key so lines cannot reference an account of another company.
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_account_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_account_tenant_company",
            ondelete="CASCADE",
        ),
        CheckConstraint(ACCOUNT_TYPE_CHECK, name="ck_account_type_allowed"),
        Index("ix_account_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_postable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )


class PostingMapping(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """Effective-dated account mapping for one posting role (report 20 §8).

    A row says: from `valid_from`, this posting role posts to this account.
    Updating a mapping closes the previous row (valid_to = new start - 1) and
    inserts a new one, so reports and audits can reconstruct which account a
    document would have used on its own date.
    """

    __tablename__ = "posting_mapping"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "key",
            "valid_from",
            name="uq_posting_mapping_tenant_company_key_from",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_posting_mapping_account",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_posting_mapping_valid_range",
        ),
        Index("ix_posting_mapping_lookup", "tenant_id", "company_id", "key", "valid_from"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class FiscalYear(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A company's accounting year; periods live inside it."""

    __tablename__ = "fiscal_year"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_fiscal_year_tenant_company_code"
        ),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_fiscal_year_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_fiscal_year_tenant_company",
            ondelete="CASCADE",
        ),
        CheckConstraint("end_date > start_date", name="ck_fiscal_year_dates_ordered"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_fiscal_year_status_allowed"),
        Index("ix_fiscal_year_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )


class AccountingPeriod(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A month-like window inside a fiscal year; postings need it open."""

    __tablename__ = "accounting_period"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "fiscal_year_id",
            "number",
            name="uq_accounting_period_tenant_company_number",
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_accounting_period_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "fiscal_year_id"],
            ["fiscal_year.tenant_id", "fiscal_year.company_id", "fiscal_year.id"],
            name="fk_accounting_period_fiscal_year",
            ondelete="CASCADE",
        ),
        CheckConstraint("end_date >= start_date", name="ck_accounting_period_dates_ordered"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_accounting_period_status_allowed"),
        Index("ix_accounting_period_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    fiscal_year_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="open",
        server_default=text("'open'"),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )


class JournalEntry(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A balanced journal entry; posted entries are immutable at the database."""

    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_journal_entry_tenant_company_number"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_journal_entry_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "period_id"],
            ["accounting_period.tenant_id", "accounting_period.company_id", "accounting_period.id"],
            name="fk_journal_entry_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "reverses_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_entry_reverses",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_journal_entry_status_allowed"),
        Index("ix_journal_entry_tenant_company_date", "tenant_id", "company_id", "entry_date"),
        Index("ix_journal_entry_tenant_company_source", "tenant_id", "company_id", "source_type"),
        # One source document can produce at most one journal entry.
        Index(
            "uq_journal_entry_source",
            "tenant_id",
            "company_id",
            "source_type",
            "source_id",
            unique=True,
            postgresql_where=text("source_type IS NOT NULL"),
        ),
        # An entry can be reversed at most once; the reversal points at it.
        Index(
            "uq_journal_entry_reversal",
            "tenant_id",
            "company_id",
            "reverses_entry_id",
            unique=True,
            postgresql_where=text("reverses_entry_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    period_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    reverses_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )


class JournalLine(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, Base):
    """One debit or credit line of a journal entry, in minor currency units."""

    __tablename__ = "journal_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "entry_id",
            "line_no",
            name="uq_journal_line_tenant_company_entry_line",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_line_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_journal_line_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint("debit_minor >= 0", name="ck_journal_line_debit_non_negative"),
        CheckConstraint("credit_minor >= 0", name="ck_journal_line_credit_non_negative"),
        CheckConstraint(
            "(debit_minor > 0) <> (credit_minor > 0)",
            name="ck_journal_line_one_side_positive",
        ),
        Index("ix_journal_line_tenant_company_entry", "tenant_id", "company_id", "entry_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    entry_id: Mapped[UUID] = mapped_column(nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    debit_minor: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    credit_minor: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)


class JournalEntryDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Links a journal entry to the source document(s) it records."""

    __tablename__ = "journal_entry_document"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "entry_id",
            "document_type",
            "document_id",
            name="uq_journal_entry_document_link",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_journal_entry_document_entry",
            ondelete="CASCADE",
        ),
        Index("ix_journal_entry_document_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    entry_id: Mapped[UUID] = mapped_column(nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)


class NumberSequence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A locked counter that hands out document numbers per company and scope.

    The row is updated with an atomic upsert so concurrent postings serialize on
    it and no two entries receive the same number.
    """

    __tablename__ = "number_sequence"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "scope",
            "period_key",
            name="uq_number_sequence_tenant_company_scope_period",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_number_sequence_tenant_company",
            ondelete="CASCADE",
        ),
        CheckConstraint("next_value >= 0", name="ck_number_sequence_next_value_non_negative"),
        Index("ix_number_sequence_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(50), nullable=False)
    period_key: Mapped[str] = mapped_column(String(20), nullable=False)
    next_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


class IdempotencyRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A client-supplied key mapped to the journal entry its first call created."""

    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "key",
            name="uq_idempotency_record_tenant_company_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_idempotency_record_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_idempotency_record_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_idempotency_record_status_allowed",
        ),
        Index("ix_idempotency_record_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="in_progress",
        server_default=text("'in_progress'"),
    )
