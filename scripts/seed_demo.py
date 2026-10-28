"""Seed the local demo tenant used by the phase-0 web shell.

Run from the repository root: `uv run python scripts/seed_demo.py`.

The script is idempotent: if the `demo` tenant already exists it prints the
credentials and changes nothing. It talks to the database configured by
`MAHMOUD_DATABASE_URL` (development default) and uses the application services for
the sample documents, so the seeded data obeys the same invariants as real data.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings  # noqa: E402
from app.modules.cashbank.models import CashBankAccount  # noqa: E402
from app.modules.cashbank.service import (  # noqa: E402
    AllocationInput,
    BankStatementLineInput,
    CashTransferInput,
    ChequeInput,
    PaymentInput,
    ReceiptInput,
    create_cash_transfer,
    create_cheque,
    create_payment,
    create_receipt,
    deposit_cheque,
    import_bank_statement_lines,
    match_bank_statement_line,
)
from app.modules.catalog.models import (  # noqa: E402
    Customer,
    Item,
    Supplier,
    TaxCode,
    TaxCodeRate,
)
from app.modules.identity.models import (  # noqa: E402
    AppUser,
    Company,
    Membership,
    MembershipRole,
    Tenant,
)
from app.modules.identity.security import hash_password  # noqa: E402
from app.modules.identity.seeding import seed_tenant_roles  # noqa: E402
from app.modules.ledger.models import AccountingPeriod, FiscalYear  # noqa: E402
from app.modules.ledger.seeding import seed_company_chart  # noqa: E402
from app.modules.purchasing.debit_notes import (  # noqa: E402
    DebitNoteInput,
    DebitNoteLineInput,
    create_debit_note,
)
from app.modules.purchasing.service import (  # noqa: E402
    PurchaseBillInput,
    PurchaseLineInput,
    create_purchase_bill,
)
from app.modules.sales.credit_notes import (  # noqa: E402
    CreditNoteInput,
    CreditNoteLineInput,
    create_credit_note,
)
from app.modules.sales.service import (  # noqa: E402
    SalesInvoiceInput,
    SalesLineInput,
    create_sales_invoice,
)
from app.shared.unit_of_work import UnitOfWork  # noqa: E402

DEMO_TENANT = "demo"
DEMO_EMAIL = "demo@almahmoud.local"
DEMO_PASSWORD = "Al-Mahmoud-Demo-2026"  # noqa: S105 - documented demo credential
COMPANY_CODE = "MAIN"
YEAR = 2026


def _print_credentials() -> None:
    print("demo tenant already present")
    print(f"tenant={DEMO_TENANT}")
    print(f"email={DEMO_EMAIL}")
    print(f"password={DEMO_PASSWORD}")


async def _align_demo_mfa_policy(engine: AsyncEngine, tenant_id: UUID) -> None:
    """Keep an existing demo tenant password-only, like a freshly seeded one."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE tenant SET mfa_sensitive_required = false, "
                "mfa_posting_required = false WHERE id = :id"
            ),
            {"id": str(tenant_id)},
        )


async def _ensure_reference(engine: AsyncEngine, tenant_id: UUID) -> None:
    """Idempotently add chart accounts and posting mappings introduced after first seed.

    The chart is data seeded through the service layer, so an existing demo
    tenant picks up new posting roles here rather than through a migration.
    """
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        company_id = (
            await session.execute(
                select(Company.id).where(
                    Company.tenant_id == tenant_id, Company.code == COMPANY_CODE
                )
            )
        ).scalar_one()
        await seed_company_chart(session, tenant_id=tenant_id, company_id=company_id)


async def _find_tenant(engine: AsyncEngine) -> UUID | None:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": DEMO_TENANT}
            )
        ).scalar_one_or_none()


async def _seed_reference(engine: AsyncEngine) -> tuple[UUID, UUID, UUID, dict[str, UUID]]:
    """Create the tenant, company, user, chart and periods in one transaction."""
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        tenant = Tenant(
            name="Al-Mahmoud Demo",
            slug=DEMO_TENANT,
            # The public walkthrough signs in with one password, so both MFA
            # obligations are explicitly off for this demo tenant. Production
            # defaults stay on; the switches are data and audited when changed.
            mfa_sensitive_required=False,
            mfa_posting_required=False,
        )
        session.add(tenant)
        await session.flush()
        company = Company(
            tenant_id=tenant.id,
            code=COMPANY_CODE,
            name_ar="شركة المحمود التجريبية",
            name_en="Al-Mahmoud Demo Company",
            base_currency="USD",
        )
        user = AppUser(
            tenant_id=tenant.id,
            email=DEMO_EMAIL,
            full_name="مدير النظام",
            password_hash=hash_password(DEMO_PASSWORD),
        )
        session.add_all([company, user])
        await session.flush()
        roles = await seed_tenant_roles(session, tenant.id)
        membership = Membership(
            tenant_id=tenant.id,
            app_user_id=user.id,
            company_id=company.id,
        )
        session.add(membership)
        await session.flush()
        session.add(
            MembershipRole(
                tenant_id=tenant.id,
                membership_id=membership.id,
                role_id=roles["tenant_admin"].id,
            )
        )
        chart = await seed_company_chart(session, tenant_id=tenant.id, company_id=company.id)
        year = FiscalYear(
            tenant_id=tenant.id,
            company_id=company.id,
            code=f"FY{YEAR}",
            name_ar=f"السنة المالية {YEAR}",
            start_date=date(YEAR, 1, 1),
            end_date=date(YEAR, 12, 31),
        )
        session.add(year)
        await session.flush()
        for month in range(1, 13):
            start = date(YEAR, month, 1)
            end = date(YEAR + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
            session.add(
                AccountingPeriod(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    fiscal_year_id=year.id,
                    number=month,
                    start_date=start,
                    end_date=end,
                )
            )
        customers = {
            "C-100": Customer(
                tenant_id=tenant.id,
                company_id=company.id,
                code="C-100",
                name_ar="شركة النور للتجارة",
                name_en="Al Noor Trading",
                tax_number="3001234567",
            ),
            "C-200": Customer(
                tenant_id=tenant.id,
                company_id=company.id,
                code="C-200",
                name_ar="مؤسسة الأمل",
                name_en="Al Amal Est.",
            ),
            "C-300": Customer(
                tenant_id=tenant.id,
                company_id=company.id,
                code="C-300",
                name_ar="متجر الوفاء",
                name_en="Al Wafa Store",
            ),
        }
        suppliers = {
            "S-100": Supplier(
                tenant_id=tenant.id,
                company_id=company.id,
                code="S-100",
                name_ar="مورد الشرق",
                name_en="East Supplier",
            ),
        }
        tax_codes = {
            "VAT15": TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="VAT15",
                name_ar="ضريبة القيمة المضافة 15%",
                rate_basis_points=1500,
            ),
            "VAT0": TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="VAT0",
                name_ar="معفاة من الضريبة",
                rate_basis_points=0,
            ),
            "WHT5": TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="WHT5",
                name_ar="ضريبة مستقطعة 5%",
                rate_basis_points=500,
                kind="withholding",
            ),
        }
        items = {
            "IT-1": Item(
                tenant_id=tenant.id,
                company_id=company.id,
                code="IT-1",
                name_ar="جهاز حاسوب مكتبي",
                name_en="Desktop computer",
                unit_of_measure="piece",
            ),
            "IT-2": Item(
                tenant_id=tenant.id,
                company_id=company.id,
                code="IT-2",
                name_ar="طابعة ليزر",
                name_en="Laser printer",
                unit_of_measure="piece",
            ),
        }
        session.add_all(
            [*customers.values(), *suppliers.values(), *tax_codes.values(), *items.values()]
        )
        await session.flush()
        for tax_code in tax_codes.values():
            session.add(
                TaxCodeRate(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    tax_code_id=tax_code.id,
                    rate_basis_points=tax_code.rate_basis_points,
                    valid_from=date(2000, 1, 1),
                )
            )
        await session.flush()
        cash = CashBankAccount(
            tenant_id=tenant.id,
            company_id=company.id,
            code="CASH-1",
            name_ar="الصندوق الرئيسي",
            name_en="Main cash box",
            kind="cash",
            ledger_account_id=chart["1000"].id,
            currency="USD",
        )
        bank = CashBankAccount(
            tenant_id=tenant.id,
            company_id=company.id,
            code="BANK-1",
            name_ar="الحساب البنكي الرئيسي",
            name_en="Main bank account",
            kind="bank",
            ledger_account_id=chart["1010"].id,
            currency="USD",
        )
        session.add_all([cash, bank])
        await session.flush()
        ids = {
            "company": company.id,
            "user": user.id,
            "customer": customers["C-100"].id,
            "customer2": customers["C-200"].id,
            "supplier": suppliers["S-100"].id,
            "tax": tax_codes["VAT15"].id,
            "withholding": tax_codes["WHT5"].id,
            "item1": items["IT-1"].id,
            "item2": items["IT-2"].id,
            "cash": cash.id,
            "bank": bank.id,
        }
        return ids["company"], ids["user"], ids["customer"], ids


async def _seed_documents(
    engine: AsyncEngine,
    *,
    company_id: UUID,
    user_id: UUID,
    ids: dict[str, UUID],
) -> None:
    unit_of_work = UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
    tenant_id = await unit_of_work.run(
        lambda session: _tenant_of(session, company_id), tenant_id=None
    )
    bill = await create_purchase_bill(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=PurchaseBillInput(
            company_id=company_id,
            supplier_id=ids["supplier"],
            bill_date=date(YEAR, 1, 10),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            lines=(
                PurchaseLineInput(
                    description="Desktop computers",
                    quantity=Decimal("10"),
                    unit_price_minor=100000,
                    item_id=ids["item1"],
                    tax_code_id=ids["tax"],
                ),
                PurchaseLineInput(
                    description="Laser printers",
                    quantity=Decimal("5"),
                    unit_price_minor=200000,
                    item_id=ids["item2"],
                    tax_code_id=ids["tax"],
                ),
            ),
        ),
    )
    invoice = await create_sales_invoice(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=SalesInvoiceInput(
            company_id=company_id,
            customer_id=ids["customer"],
            invoice_date=date(YEAR, 1, 15),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            lines=(
                SalesLineInput(
                    description="Desktop computer",
                    quantity=Decimal("2"),
                    unit_price_minor=150000,
                    item_id=ids["item1"],
                    tax_code_id=ids["tax"],
                ),
                SalesLineInput(
                    description="Laser printer",
                    quantity=Decimal("1"),
                    unit_price_minor=300000,
                    item_id=ids["item2"],
                    tax_code_id=ids["tax"],
                ),
            ),
        ),
    )
    await create_receipt(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=ReceiptInput(
            company_id=company_id,
            customer_id=ids["customer"],
            cash_bank_account_id=ids["cash"],
            receipt_date=date(YEAR, 1, 25),
            amount_minor=200000,
            currency="USD",
            post=True,
            allocations=(AllocationInput(document_id=invoice.invoice.id, amount_minor=200000),),
        ),
    )
    payment = await create_payment(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=PaymentInput(
            company_id=company_id,
            supplier_id=ids["supplier"],
            cash_bank_account_id=ids["bank"],
            payment_date=date(YEAR, 1, 28),
            amount_minor=500000,
            currency="USD",
            post=True,
            withholding_tax_code_id=ids["withholding"],
            allocations=(AllocationInput(document_id=bill.bill.id, amount_minor=500000),),
        ),
    )
    await create_sales_invoice(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=SalesInvoiceInput(
            company_id=company_id,
            customer_id=ids["customer2"],
            invoice_date=date(YEAR, 1, 18),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            lines=(
                SalesLineInput(
                    description="Consulting service",
                    quantity=Decimal("1"),
                    unit_price_minor=120000,
                ),
            ),
        ),
    )
    await create_sales_invoice(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=SalesInvoiceInput(
            company_id=company_id,
            customer_id=ids["customer"],
            invoice_date=date(YEAR, 1, 20),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=False,
            lines=(
                SalesLineInput(
                    description="Laser printer (draft)",
                    quantity=Decimal("1"),
                    unit_price_minor=300000,
                    item_id=ids["item2"],
                    tax_code_id=ids["tax"],
                ),
            ),
        ),
    )
    await create_credit_note(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=CreditNoteInput(
            company_id=company_id,
            customer_id=ids["customer"],
            invoice_id=invoice.invoice.id,
            credit_note_date=date(YEAR, 1, 22),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            memo="Returned one desktop computer",
            lines=(
                CreditNoteLineInput(
                    description="Desktop computer returned",
                    quantity=Decimal("1"),
                    unit_price_minor=150000,
                    item_id=ids["item1"],
                    tax_code_id=ids["tax"],
                    invoice_line_id=invoice.lines[0].id,
                ),
            ),
        ),
    )
    await create_debit_note(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=DebitNoteInput(
            company_id=company_id,
            supplier_id=ids["supplier"],
            bill_id=bill.bill.id,
            debit_note_date=date(YEAR, 1, 23),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            memo="Returned one laser printer",
            lines=(
                DebitNoteLineInput(
                    description="Laser printer returned",
                    quantity=Decimal("1"),
                    unit_price_minor=200000,
                    item_id=ids["item2"],
                    tax_code_id=ids["tax"],
                    bill_line_id=bill.lines[1].id,
                ),
            ),
        ),
    )
    await create_cash_transfer(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=CashTransferInput(
            company_id=company_id,
            transfer_date=date(YEAR, 1, 5),
            from_account_id=ids["cash"],
            to_account_id=ids["bank"],
            amount_minor=100000,
            currency="USD",
            memo="Cash deposited into the bank",
        ),
    )
    second_invoice = await create_sales_invoice(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=SalesInvoiceInput(
            company_id=company_id,
            customer_id=ids["customer2"],
            invoice_date=date(YEAR, 1, 21),
            currency="USD",
            exchange_rate=Decimal("1"),
            post=True,
            lines=(
                SalesLineInput(
                    description="Annual support contract",
                    quantity=Decimal("1"),
                    unit_price_minor=80000,
                ),
            ),
        ),
    )
    received = await create_cheque(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=ChequeInput(
            company_id=company_id,
            direction="received",
            number="CH-1001",
            cash_bank_account_id=ids["bank"],
            issue_date=date(YEAR, 1, 24),
            due_date=date(YEAR, 2, 24),
            amount_minor=80000,
            customer_id=ids["customer2"],
            currency="USD",
            post=True,
            allocations=(
                AllocationInput(document_id=second_invoice.invoice.id, amount_minor=80000),
            ),
        ),
    )
    deposited = await deposit_cheque(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        company_id=company_id,
        cheque_id=received.cheque.id,
        event_date=date(YEAR, 1, 29),
        notes="Deposited at the bank",
    )
    issued = await create_cheque(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        request=ChequeInput(
            company_id=company_id,
            direction="issued",
            number="CH-2001",
            cash_bank_account_id=ids["bank"],
            issue_date=date(YEAR, 1, 30),
            due_date=date(YEAR, 2, 28),
            amount_minor=300000,
            supplier_id=ids["supplier"],
            currency="USD",
            post=True,
            allocations=(AllocationInput(document_id=bill.bill.id, amount_minor=300000),),
        ),
    )
    statement_lines = await import_bank_statement_lines(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        company_id=company_id,
        cash_bank_account_id=ids["bank"],
        currency="USD",
        lines=(
            BankStatementLineInput(
                statement_date=date(YEAR, 1, 29),
                description="Cheque deposit",
                amount_minor=80000,
                reference="CH-1001",
            ),
            BankStatementLineInput(
                statement_date=date(YEAR, 1, 28),
                description="Supplier payment",
                amount_minor=-475000,
                reference="PY-2026-000001",
            ),
            BankStatementLineInput(
                statement_date=date(YEAR, 1, 31),
                description="Bank charges",
                amount_minor=-2500,
                reference="FEE-31",
            ),
        ),
    )
    await match_bank_statement_line(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        company_id=company_id,
        line_id=statement_lines[0].id,
        source_type="cheque_event",
        source_id=deposited.events[-1].id,
    )
    await match_bank_statement_line(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        company_id=company_id,
        line_id=statement_lines[1].id,
        source_type="supplier_payment",
        source_id=payment.payment.id,
    )
    _ = issued


async def _tenant_of(session: AsyncSession, company_id: UUID) -> UUID:
    result = await session.execute(select(Company.tenant_id).where(Company.id == company_id))
    return result.scalar_one()


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        existing = await _find_tenant(engine)
        if existing is not None:
            await _align_demo_mfa_policy(engine, existing)
            await _ensure_reference(engine, existing)
            _print_credentials()
            return
        company_id, user_id, _customer_id, ids = await _seed_reference(engine)
        await _seed_documents(engine, company_id=company_id, user_id=user_id, ids=ids)
    finally:
        await engine.dispose()
    print("demo tenant created")
    _print_credentials()


if __name__ == "__main__":
    asyncio.run(main())
