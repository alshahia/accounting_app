"""Sales invoices with lines and tax (phase 0, day 7).

Money is integer minor units in the document currency; every document also
carries base-currency amounts converted at its own exchange rate. Posting is
done by the sales service in one transaction with its journal entry and any
stock issues, so an invoice is never half-posted.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import ActorMixin, Base, RowVersionMixin, TimestampMixin, UUIDPrimaryKeyMixin


class SalesInvoice(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A customer invoice, draft until posted."""

    __tablename__ = "sales_invoice"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_sales_invoice_tenant_company_number"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_sales_invoice_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_sales_invoice_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_sales_invoice_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_sales_invoice_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "status IN ('draft', 'posted', 'part_paid', 'paid')",
            name="ck_sales_invoice_status_allowed",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_sales_invoice_currency_iso"),
        CheckConstraint("exchange_rate > 0", name="ck_sales_invoice_exchange_rate_positive"),
        CheckConstraint("subtotal_minor >= 0", name="ck_sales_invoice_subtotal_non_negative"),
        CheckConstraint("tax_minor >= 0", name="ck_sales_invoice_tax_non_negative"),
        CheckConstraint("total_minor >= 0", name="ck_sales_invoice_total_non_negative"),
        CheckConstraint(
            "total_minor = subtotal_minor + tax_minor", name="ck_sales_invoice_total_consistent"
        ),
        CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_sales_invoice_base_total_consistent",
        ),
        Index("ix_sales_invoice_tenant_company_date", "tenant_id", "company_id", "invoice_date"),
        Index("ix_sales_invoice_customer", "tenant_id", "company_id", "customer_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IQD")
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default=text("'draft'")
    )
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    cogs_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class SalesInvoiceLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One invoice line; tax is resolved from its tax code when created."""

    __tablename__ = "sales_invoice_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "invoice_id", "line_number", name="uq_sales_invoice_line_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_sales_invoice_line_tenant_id"),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_sales_invoice_line_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_sales_invoice_line_invoice",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_sales_invoice_line_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_sales_invoice_line_tax_code",
        ),
        CheckConstraint("quantity > 0", name="ck_sales_invoice_line_quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="ck_sales_invoice_line_price_non_negative"),
        CheckConstraint(
            "line_subtotal_minor >= 0 AND line_tax_minor >= 0",
            name="ck_sales_invoice_line_amounts_non_negative",
        ),
        CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_sales_invoice_line_total_consistent",
        ),
        Index("ix_sales_invoice_line_invoice", "tenant_id", "invoice_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(nullable=False)
    line_number: Mapped[int] = mapped_column(nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(nullable=True)
    tax_code_id: Mapped[UUID | None] = mapped_column(nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_line_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_line_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class SalesCreditNote(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A customer credit note; draft until posted, optionally against an invoice."""

    __tablename__ = "sales_credit_note"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "number",
            name="uq_sales_credit_note_tenant_company_number",
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_sales_credit_note_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_sales_credit_note_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_sales_credit_note_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_sales_credit_note_invoice",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_sales_credit_note_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_sales_credit_note_status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_sales_credit_note_currency_iso"),
        CheckConstraint("exchange_rate > 0", name="ck_sales_credit_note_exchange_rate_positive"),
        CheckConstraint(
            "subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0",
            name="ck_sales_credit_note_amounts_non_negative",
        ),
        CheckConstraint(
            "total_minor = subtotal_minor + tax_minor",
            name="ck_sales_credit_note_total_consistent",
        ),
        CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_sales_credit_note_base_total_consistent",
        ),
        Index(
            "ix_sales_credit_note_tenant_company_date",
            "tenant_id",
            "company_id",
            "credit_note_date",
        ),
        Index("ix_sales_credit_note_customer", "tenant_id", "company_id", "customer_id"),
        Index("ix_sales_credit_note_invoice", "tenant_id", "company_id", "invoice_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_id: Mapped[UUID | None] = mapped_column(nullable=True)
    credit_note_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="IQD")
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default=text("'draft'")
    )
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    base_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class SalesCreditNoteLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One credit-note line; item lines link the invoice line they return."""

    __tablename__ = "sales_credit_note_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "credit_note_id", "line_number", name="uq_sales_credit_note_line_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_sales_credit_note_line_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "credit_note_id"],
            [
                "sales_credit_note.tenant_id",
                "sales_credit_note.company_id",
                "sales_credit_note.id",
            ],
            name="fk_sales_credit_note_line_note",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_line_id"],
            [
                "sales_invoice_line.tenant_id",
                "sales_invoice_line.company_id",
                "sales_invoice_line.id",
            ],
            name="fk_sales_credit_note_line_invoice_line",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_sales_credit_note_line_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_sales_credit_note_line_tax_code",
        ),
        CheckConstraint("quantity > 0", name="ck_sales_credit_note_line_quantity_positive"),
        CheckConstraint(
            "unit_price_minor >= 0", name="ck_sales_credit_note_line_price_non_negative"
        ),
        CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_sales_credit_note_line_total_consistent",
        ),
        Index("ix_sales_credit_note_line_note", "tenant_id", "credit_note_id"),
        Index(
            "ix_sales_credit_note_line_invoice_line",
            "tenant_id",
            "company_id",
            "invoice_line_id",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    credit_note_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_line_id: Mapped[UUID | None] = mapped_column(nullable=True)
    line_number: Mapped[int] = mapped_column(nullable=False)
    item_id: Mapped[UUID | None] = mapped_column(nullable=True)
    tax_code_id: Mapped[UUID | None] = mapped_column(nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_line_subtotal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_line_tax_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
