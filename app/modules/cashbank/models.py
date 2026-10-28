"""Cash and bank accounts, customer receipts and supplier payments (phase 1, day 11).

A receipt or payment is a document that moves money through one cash/bank
ledger account and settles customer invoices or supplier bills. Allocations are
stored in the document currency; the base-currency amount posted against the
receivable or payable is converted at the settled document's own exchange rate,
so the settlement keeps the document's historical value. The difference between
that base value and the money moved at the receipt's rate is posted as exchange
gain or loss, never silently absorbed.

Posted documents are financial records: they are not deleted, and their content
is not edited. Draft receipts and payments exist so a clerk can prepare the
allocation and an accountant can post it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class CashBankAccount(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A cash box or bank account, linked to one postable ledger account."""

    __tablename__ = "cash_bank_account"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_cash_bank_account_tenant_company_code"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_cash_bank_account_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_cash_bank_account_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "ledger_account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_cash_bank_account_ledger_account",
        ),
        CheckConstraint("kind IN ('cash', 'bank')", name="kind_allowed"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        Index("ix_cash_bank_account_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    ledger_account_id: Mapped[UUID] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Receipt(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """Money received from a customer, allocated or held as an advance."""

    __tablename__ = "receipt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_receipt_tenant_company_number"
        ),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_receipt_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_receipt_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_receipt_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_receipt_cash_bank_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_receipt_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="status_allowed"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("base_amount_minor > 0", name="base_amount_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "withholding_tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_receipt_withholding_tax_code",
        ),
        CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="unallocated_range",
        ),
        CheckConstraint(
            "withholding_amount_minor >= 0 AND withholding_amount_minor <= amount_minor",
            name="ck_receipt_withholding_range",
        ),
        CheckConstraint(
            "base_withholding_minor >= 0 AND base_withholding_minor <= base_amount_minor",
            name="ck_receipt_base_withholding_range",
        ),
        Index("ix_receipt_tenant_company_date", "tenant_id", "company_id", "receipt_date"),
        Index("ix_receipt_customer", "tenant_id", "company_id", "customer_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[UUID] = mapped_column(nullable=False)
    cash_bank_account_id: Mapped[UUID] = mapped_column(nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unallocated_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    withholding_tax_code_id: Mapped[UUID | None] = mapped_column(nullable=True)
    withholding_base_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    withholding_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    base_withholding_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default=text("'draft'")
    )
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class ReceiptAllocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One receipt amount applied to one customer invoice."""

    __tablename__ = "receipt_allocation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "receipt_id", "invoice_id", name="uq_receipt_allocation_receipt_invoice"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_receipt_allocation_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "receipt_id"],
            ["receipt.tenant_id", "receipt.company_id", "receipt.id"],
            name="fk_receipt_allocation_receipt",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_receipt_allocation_invoice",
        ),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0", name="base_amount_non_negative"
        ),
        Index("ix_receipt_allocation_invoice", "tenant_id", "company_id", "invoice_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settlement_base_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SupplierPayment(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """Money paid to a supplier, allocated to bills or held as an advance."""

    __tablename__ = "supplier_payment"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_supplier_payment_tenant_company_number"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_supplier_payment_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_supplier_payment_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_supplier_payment_supplier",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_supplier_payment_cash_bank_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_supplier_payment_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="status_allowed"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("base_amount_minor > 0", name="base_amount_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "withholding_tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_supplier_payment_withholding_tax_code",
        ),
        CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="unallocated_range",
        ),
        CheckConstraint(
            "withholding_amount_minor >= 0 AND withholding_amount_minor <= amount_minor",
            name="ck_supplier_payment_withholding_range",
        ),
        CheckConstraint(
            "base_withholding_minor >= 0 AND base_withholding_minor <= base_amount_minor",
            name="ck_supplier_payment_base_withholding_range",
        ),
        Index(
            "ix_supplier_payment_tenant_company_date",
            "tenant_id",
            "company_id",
            "payment_date",
        ),
        Index("ix_supplier_payment_supplier", "tenant_id", "company_id", "supplier_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[UUID] = mapped_column(nullable=False)
    cash_bank_account_id: Mapped[UUID] = mapped_column(nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unallocated_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    withholding_tax_code_id: Mapped[UUID | None] = mapped_column(nullable=True)
    withholding_base_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    withholding_amount_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    base_withholding_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default=text("'draft'")
    )
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class SupplierPaymentAllocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One payment amount applied to one supplier bill."""

    __tablename__ = "supplier_payment_allocation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "payment_id",
            "bill_id",
            name="uq_supplier_payment_allocation_payment_bill",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_supplier_payment_allocation_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "payment_id"],
            ["supplier_payment.tenant_id", "supplier_payment.company_id", "supplier_payment.id"],
            name="fk_supplier_payment_allocation_payment",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_supplier_payment_allocation_bill",
        ),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0", name="base_amount_non_negative"
        ),
        Index("ix_supplier_payment_allocation_bill", "tenant_id", "company_id", "bill_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    payment_id: Mapped[UUID] = mapped_column(nullable=False)
    bill_id: Mapped[UUID] = mapped_column(nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settlement_base_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CashTransfer(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A posted movement of money between two cash/bank accounts."""

    __tablename__ = "cash_transfer"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_cash_transfer_tenant_company_number"
        ),
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_cash_transfer_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_cash_transfer_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "from_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_cash_transfer_from_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "to_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_cash_transfer_to_account",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_cash_transfer_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint("status IN ('posted')", name="status_allowed"),
        CheckConstraint("from_account_id <> to_account_id", name="accounts_differ"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("base_amount_minor > 0", name="base_amount_positive"),
        Index("ix_cash_transfer_tenant_company_date", "tenant_id", "company_id", "transfer_date"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_account_id: Mapped[UUID] = mapped_column(nullable=False)
    to_account_id: Mapped[UUID] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="posted", server_default=text("'posted'")
    )
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_by: Mapped[UUID | None] = mapped_column(nullable=True)


class Cheque(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A cheque received from a customer or issued to a supplier, with its lifecycle."""

    __tablename__ = "cheque"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_cheque_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_cheque_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_cheque_customer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_cheque_supplier",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_cheque_cash_bank_account",
        ),
        CheckConstraint("direction IN ('received', 'issued')", name="direction_allowed"),
        CheckConstraint(
            "status IN ('in_hand', 'deposited', 'cleared', 'issued', 'bounced', 'cancelled')",
            name="status_allowed",
        ),
        CheckConstraint(
            "(direction = 'received' AND customer_id IS NOT NULL AND supplier_id IS NULL) "
            "OR (direction = 'issued' AND supplier_id IS NOT NULL AND customer_id IS NULL)",
            name="party_matches_direction",
        ),
        CheckConstraint(
            "(direction = 'received' AND status IN "
            "('in_hand', 'deposited', 'cleared', 'bounced', 'cancelled')) "
            "OR (direction = 'issued' AND status IN ('issued', 'cleared', 'bounced', 'cancelled'))",
            name="status_matches_direction",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("base_amount_minor > 0", name="base_amount_positive"),
        CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="unallocated_range",
        ),
        CheckConstraint("due_date >= issue_date", name="due_date_after_issue"),
        Index("ix_cheque_tenant_company_status", "tenant_id", "company_id", "status"),
        Index("ix_cheque_tenant_company_due", "tenant_id", "company_id", "due_date"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[UUID | None] = mapped_column(nullable=True)
    supplier_id: Mapped[UUID | None] = mapped_column(nullable=True)
    cash_bank_account_id: Mapped[UUID] = mapped_column(nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    party_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("1"), server_default=text("1")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unallocated_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)


class ChequeAllocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cheque amount applied to one invoice (received) or bill (issued)."""

    __tablename__ = "cheque_allocation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_cheque_allocation_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cheque_id"],
            ["cheque.tenant_id", "cheque.company_id", "cheque.id"],
            name="fk_cheque_allocation_cheque",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_cheque_allocation_invoice",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_cheque_allocation_bill",
        ),
        CheckConstraint(
            "(invoice_id IS NOT NULL AND bill_id IS NULL) OR "
            "(invoice_id IS NULL AND bill_id IS NOT NULL)",
            name="one_document",
        ),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0", name="base_amount_non_negative"
        ),
        CheckConstraint("is_reversed = false OR reversed_at IS NOT NULL", name="reversal_stamped"),
        Index("ix_cheque_allocation_invoice", "tenant_id", "company_id", "invoice_id"),
        Index("ix_cheque_allocation_bill", "tenant_id", "company_id", "bill_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    cheque_id: Mapped[UUID] = mapped_column(nullable=False)
    invoice_id: Mapped[UUID | None] = mapped_column(nullable=True)
    bill_id: Mapped[UUID | None] = mapped_column(nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settlement_base_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_reversed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChequeEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One posted lifecycle event for a cheque, with the journal entry it produced."""

    __tablename__ = "cheque_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_cheque_event_tenant_id"),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_cheque_event_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cheque_id"],
            ["cheque.tenant_id", "cheque.company_id", "cheque.id"],
            name="fk_cheque_event_cheque",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_cheque_event_journal_entry",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "event_type IN ('received', 'deposited', 'cleared', 'bounced', 'issued', 'cancelled')",
            name="event_type_allowed",
        ),
        Index("ix_cheque_event_cheque", "tenant_id", "company_id", "cheque_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False)
    cheque_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)


class BankStatementLine(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """One imported bank statement line, matched or left visible as unmatched."""

    __tablename__ = "bank_statement_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_bank_statement_line_tenant_company_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_bank_statement_line_tenant_company",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_bank_statement_line_cash_bank_account",
        ),
        CheckConstraint("status IN ('unmatched', 'matched')", name="status_allowed"),
        CheckConstraint("amount_minor <> 0", name="amount_non_zero"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint(
            "(status = 'unmatched' AND matched_source_type IS NULL AND matched_source_id IS NULL) "
            "OR (status = 'matched' AND matched_source_type IS NOT NULL "
            "AND matched_source_id IS NOT NULL)",
            name="match_fields_consistent",
        ),
        Index(
            "ix_bank_statement_line_account_date",
            "tenant_id",
            "company_id",
            "cash_bank_account_id",
            "statement_date",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    cash_bank_account_id: Mapped[UUID] = mapped_column(nullable=False)
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unmatched", server_default=text("'unmatched'")
    )
    matched_source_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    matched_source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_by: Mapped[UUID | None] = mapped_column(nullable=True)
