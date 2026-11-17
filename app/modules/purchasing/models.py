"""Purchase bills with lines and tax (phase 0, day 7).

A posted bill debits inventory (or an expense line) and input VAT and credits
accounts payable in base currency, and updates each item's weighted-average
stock in the same transaction.
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


class PurchaseBill(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A supplier bill, draft until posted."""

    __tablename__ = "purchase_bill"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_purchase_bill_tenant_company_number"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_purchase_bill_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_purchase_bill_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_purchase_bill_supplier",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_purchase_bill_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "status IN ('draft', 'posted', 'part_paid', 'paid')",
            name="ck_purchase_bill_status_allowed",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_purchase_bill_currency_iso"),
        CheckConstraint("exchange_rate > 0", name="ck_purchase_bill_exchange_rate_positive"),
        CheckConstraint("subtotal_minor >= 0", name="ck_purchase_bill_subtotal_non_negative"),
        CheckConstraint("tax_minor >= 0", name="ck_purchase_bill_tax_non_negative"),
        CheckConstraint("total_minor >= 0", name="ck_purchase_bill_total_non_negative"),
        CheckConstraint(
            "total_minor = subtotal_minor + tax_minor", name="ck_purchase_bill_total_consistent"
        ),
        CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_purchase_bill_base_total_consistent",
        ),
        Index("ix_purchase_bill_tenant_company_date", "tenant_id", "company_id", "bill_date"),
        Index("ix_purchase_bill_supplier", "tenant_id", "company_id", "supplier_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[UUID] = mapped_column(nullable=False)
    bill_date: Mapped[date] = mapped_column(Date, nullable=False)
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
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class PurchaseBillLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One bill line; a stocked item receives quantity at the line's base cost."""

    __tablename__ = "purchase_bill_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "bill_id", "line_number", name="uq_purchase_bill_line_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_purchase_bill_line_tenant_id"),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_purchase_bill_line_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_purchase_bill_line_bill",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_purchase_bill_line_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_purchase_bill_line_tax_code",
        ),
        CheckConstraint("quantity > 0", name="ck_purchase_bill_line_quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="ck_purchase_bill_line_price_non_negative"),
        CheckConstraint(
            "line_subtotal_minor >= 0 AND line_tax_minor >= 0",
            name="ck_purchase_bill_line_amounts_non_negative",
        ),
        CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_purchase_bill_line_total_consistent",
        ),
        Index("ix_purchase_bill_line_bill", "tenant_id", "bill_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    bill_id: Mapped[UUID] = mapped_column(nullable=False)
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


class PurchaseDebitNote(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A supplier debit note; draft until posted, optionally against a bill."""

    __tablename__ = "purchase_debit_note"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "number",
            name="uq_purchase_debit_note_tenant_company_number",
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_purchase_debit_note_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_purchase_debit_note_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_purchase_debit_note_supplier",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_purchase_debit_note_bill",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_purchase_debit_note_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_purchase_debit_note_status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_purchase_debit_note_currency_iso"),
        CheckConstraint("exchange_rate > 0", name="ck_purchase_debit_note_exchange_rate_positive"),
        CheckConstraint(
            "subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0",
            name="ck_purchase_debit_note_amounts_non_negative",
        ),
        CheckConstraint(
            "total_minor = subtotal_minor + tax_minor",
            name="ck_purchase_debit_note_total_consistent",
        ),
        CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_purchase_debit_note_base_total_consistent",
        ),
        Index(
            "ix_purchase_debit_note_tenant_company_date",
            "tenant_id",
            "company_id",
            "debit_note_date",
        ),
        Index("ix_purchase_debit_note_supplier", "tenant_id", "company_id", "supplier_id"),
        Index("ix_purchase_debit_note_bill", "tenant_id", "company_id", "bill_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[UUID] = mapped_column(nullable=False)
    bill_id: Mapped[UUID | None] = mapped_column(nullable=True)
    debit_note_date: Mapped[date] = mapped_column(Date, nullable=False)
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


class PurchaseDebitNoteLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One debit-note line; item lines link the bill line they return."""

    __tablename__ = "purchase_debit_note_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "debit_note_id",
            "line_number",
            name="uq_purchase_debit_note_line_number",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_purchase_debit_note_line_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "debit_note_id"],
            [
                "purchase_debit_note.tenant_id",
                "purchase_debit_note.company_id",
                "purchase_debit_note.id",
            ],
            name="fk_purchase_debit_note_line_note",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_line_id"],
            [
                "purchase_bill_line.tenant_id",
                "purchase_bill_line.company_id",
                "purchase_bill_line.id",
            ],
            name="fk_purchase_debit_note_line_bill_line",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_purchase_debit_note_line_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_purchase_debit_note_line_tax_code",
        ),
        CheckConstraint("quantity > 0", name="ck_purchase_debit_note_line_quantity_positive"),
        CheckConstraint(
            "unit_price_minor >= 0", name="ck_purchase_debit_note_line_price_non_negative"
        ),
        CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_purchase_debit_note_line_total_consistent",
        ),
        Index("ix_purchase_debit_note_line_note", "tenant_id", "debit_note_id"),
        Index(
            "ix_purchase_debit_note_line_bill_line",
            "tenant_id",
            "company_id",
            "bill_line_id",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    debit_note_id: Mapped[UUID] = mapped_column(nullable=False)
    bill_line_id: Mapped[UUID | None] = mapped_column(nullable=True)
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
