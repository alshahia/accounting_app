"""Master data (phase 0, day 7): customers, suppliers, items and tax codes.

These are company-scoped reference records; amounts and rates are data, never
constants in code. Item rows also carry the current stock position so a
weighted-average receipt or issue updates stock and value in one transaction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import ActorMixin, Base, RowVersionMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A company's customer."""

    __tablename__ = "customer"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "code", name="uq_customer_tenant_company_code"),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_customer_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_customer_tenant_company",
            ondelete="CASCADE",
        ),
        Index("ix_customer_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class Supplier(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A company's supplier."""

    __tablename__ = "supplier"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "code", name="uq_supplier_tenant_company_code"),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_supplier_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_supplier_tenant_company",
            ondelete="CASCADE",
        ),
        Index("ix_supplier_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class TaxCode(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A VAT rate as basis points (1500 = 15.00%); the rate is data, not code."""

    __tablename__ = "tax_code"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "code", name="uq_tax_code_tenant_company_code"),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_tax_code_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_tax_code_tenant_company",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "rate_basis_points >= 0 AND rate_basis_points <= 10000",
            name="ck_tax_code_rate_range",
        ),
        CheckConstraint(
            "kind IN ('sales', 'purchase', 'withholding')",
            name="ck_tax_code_kind",
        ),
        Index("ix_tax_code_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    rate_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="sales", server_default=text("'sales'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class TaxCodeRate(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """One effective-dated rate for a tax code; posting resolves the rate by date."""

    __tablename__ = "tax_code_rate"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "tax_code_id",
            "valid_from",
            name="uq_tax_code_rate_tenant_company_code_from",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_tax_code_rate_tax_code",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "rate_basis_points >= 0 AND rate_basis_points <= 10000",
            name="ck_tax_code_rate_value_range",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_tax_code_rate_valid_range",
        ),
        Index("ix_tax_code_rate_lookup", "tenant_id", "company_id", "tax_code_id", "valid_from"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    tax_code_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    rate_basis_points: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Item(UUIDPrimaryKeyMixin, TimestampMixin, RowVersionMixin, ActorMixin, Base):
    """A stock item with its current quantity and value in base minor units."""

    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("tenant_id", "company_id", "code", name="uq_item_tenant_company_code"),
        UniqueConstraint("tenant_id", "company_id", "id", name="uq_item_tenant_company_id"),
        ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_item_tenant_company",
            ondelete="CASCADE",
        ),
        CheckConstraint("quantity_on_hand >= 0", name="ck_item_quantity_non_negative"),
        CheckConstraint("stock_value_minor >= 0", name="ck_item_value_non_negative"),
        Index("ix_item_tenant_company", "tenant_id", "company_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    company_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    unit_of_measure: Mapped[str] = mapped_column(String(20), nullable=False, default="piece")
    quantity_on_hand: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    stock_value_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
