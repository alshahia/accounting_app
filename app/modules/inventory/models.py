"""Stock movements and weighted-average valuation (phase 0, day 7).

Every receipt and issue writes a stock_move row and updates the item's quantity
and value. The value is always the amount posted to the inventory account for
that document, so the inventory GL balance and the sum of item values agree by
construction.
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

from app.shared.base import ActorMixin, Base, TimestampMixin, UUIDPrimaryKeyMixin


class StockMove(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One receipt or issue of an item, linked to its source document."""

    __tablename__ = "stock_move"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "document_type",
            "document_id",
            "item_id",
            "move_type",
            name="uq_stock_move_document_item_type",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_move_item",
        ),
        CheckConstraint(
            "move_type IN ('receipt', 'issue', 'count_adjustment')",
            name="ck_stock_move_type_allowed",
        ),
        CheckConstraint("quantity > 0", name="ck_stock_move_quantity_positive"),
        CheckConstraint("value_minor >= 0", name="ck_stock_move_value_non_negative"),
        Index("ix_stock_move_item", "tenant_id", "company_id", "item_id", "occurred_on"),
        Index("ix_stock_move_document", "tenant_id", "company_id", "document_type", "document_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    warehouse_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    item_id: Mapped[UUID] = mapped_column(nullable=False)
    move_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(nullable=True)


class Warehouse(UUIDPrimaryKeyMixin, TimestampMixin, ActorMixin, Base):
    """A physical stock location; one per company is the default."""

    __tablename__ = "warehouse"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_warehouse_tenant_company_code"
        ),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_warehouse_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_warehouse_tenant_company",
            ondelete="CASCADE",
        ),
        Index("ix_warehouse_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class StockBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Quantity and value of one item in one warehouse; item totals stay the sum."""

    __tablename__ = "stock_balance"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "warehouse_id",
            "item_id",
            name="uq_stock_balance_tenant_company_warehouse_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_balance_warehouse",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_balance_item",
            ondelete="CASCADE",
        ),
        CheckConstraint("quantity >= 0", name="ck_stock_balance_quantity_non_negative"),
        CheckConstraint("value_minor >= 0", name="ck_stock_balance_value_non_negative"),
        Index("ix_stock_balance_warehouse", "tenant_id", "company_id", "warehouse_id"),
        Index("ix_stock_balance_item", "tenant_id", "company_id", "item_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    warehouse_id: Mapped[UUID] = mapped_column(nullable=False)
    item_id: Mapped[UUID] = mapped_column(nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    value_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


class StockTransfer(UUIDPrimaryKeyMixin, TimestampMixin, ActorMixin, Base):
    """A posted transfer of one item between two warehouses of the same company."""

    __tablename__ = "stock_transfer"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_stock_transfer_tenant_company_number"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "from_warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_transfer_from_warehouse",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "to_warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_transfer_to_warehouse",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_transfer_item",
        ),
        CheckConstraint("quantity > 0", name="ck_stock_transfer_quantity_positive"),
        CheckConstraint(
            "from_warehouse_id <> to_warehouse_id", name="ck_stock_transfer_different_warehouses"
        ),
        Index("ix_stock_transfer_tenant_company_date", "tenant_id", "company_id", "transfer_date"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    transfer_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_warehouse_id: Mapped[UUID] = mapped_column(nullable=False)
    to_warehouse_id: Mapped[UUID] = mapped_column(nullable=False)
    item_id: Mapped[UUID] = mapped_column(nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    value_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockCount(UUIDPrimaryKeyMixin, TimestampMixin, ActorMixin, Base):
    """A physical stock count for one warehouse; drafts are editable, posting is final."""

    __tablename__ = "stock_count"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_stock_count_tenant_company_number"
        ),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_stock_count_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_count_warehouse",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_stock_count_journal_entry",
        ),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_stock_count_status"),
        Index("ix_stock_count_tenant_company_date", "tenant_id", "company_id", "count_date"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    count_date: Mapped[date] = mapped_column(Date, nullable=False)
    warehouse_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default=text("'draft'")
    )
    memo: Mapped[str | None] = mapped_column(String(300), nullable=True)
    journal_entry_id: Mapped[UUID | None] = mapped_column(nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StockCountLine(UUIDPrimaryKeyMixin, TimestampMixin, ActorMixin, Base):
    """One item's counted quantity inside a stock count, with its computed variance."""

    __tablename__ = "stock_count_line"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "stock_count_id",
            "item_id",
            name="uq_stock_count_line_tenant_company_count_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "stock_count_id"],
            ["stock_count.tenant_id", "stock_count.company_id", "stock_count.id"],
            name="fk_stock_count_line_count",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_count_line_item",
        ),
        CheckConstraint("counted_quantity >= 0", name="ck_stock_count_line_counted_non_negative"),
        CheckConstraint("system_quantity >= 0", name="ck_stock_count_line_system_non_negative"),
        Index(
            "ix_stock_count_line_count",
            "tenant_id",
            "company_id",
            "stock_count_id",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    stock_count_id: Mapped[UUID] = mapped_column(nullable=False)
    item_id: Mapped[UUID] = mapped_column(nullable=False)
    counted_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    system_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    variance_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    variance_value_minor: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default=text("0")
    )
