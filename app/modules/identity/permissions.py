"""Permission catalogue and the default role bundles (phase 0, day 3).

Permissions are named `module.action`. They are security configuration, not
business values: the catalogue is code, the assignment of roles to people is
data. A role is a named bundle inside one tenant; the same bundle ships with
every tenant and can be edited later (report 21 §6).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    "admin.user.manage": "Create, update and deactivate users",
    "admin.role.manage": "Create roles and assign permissions",
    "admin.company.manage": "Create and configure companies",
    "ledger.journal.create": "Create draft journal entries",
    "ledger.journal.post": "Post journal entries",
    "ledger.period.close": "Close and reopen accounting periods",
    "sales.invoice.create": "Create sales invoices",
    "sales.invoice.post": "Post sales invoices",
    "sales.credit_note.create": "Create customer credit notes",
    "sales.credit_note.post": "Post customer credit notes",
    "purchasing.bill.create": "Create purchase bills",
    "purchasing.bill.post": "Post purchase bills",
    "purchasing.debit_note.create": "Create supplier debit notes",
    "purchasing.debit_note.post": "Post supplier debit notes",
    "inventory.manage": "Manage items, warehouses and stock moves",
    "cashbank.manage": "Manage cash and bank accounts",
    "cashbank.receipt.create": "Create customer receipts",
    "cashbank.receipt.post": "Post customer receipts",
    "cashbank.payment.create": "Create supplier payments",
    "cashbank.payment.post": "Post supplier payments",
    "cashbank.transfer.create": "Create cash and bank transfers",
    "cashbank.transfer.post": "Post cash and bank transfers",
    "cashbank.cheque.create": "Create received and issued cheques",
    "cashbank.cheque.post": "Post cheque lifecycle events",
    "cashbank.reconcile": "Import and match bank statement lines",
    "tax.manage": "Manage tax codes and tax reports",
    "report.view": "Read financial and operational reports",
    "audit.view": "Read the audit log",
    "settings.manage": "Manage company settings and numbering",
}

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSION_DESCRIPTIONS)


@dataclass(frozen=True)
class RoleDefinition:
    """A default role: its display name and the permissions it bundles."""

    name: str
    permissions: frozenset[str]


# Deny by default: a role only has what is listed here. The auditor is
# deliberately read-only.
DEFAULT_ROLES: dict[str, RoleDefinition] = {
    "tenant_admin": RoleDefinition("Tenant administrator", ALL_PERMISSIONS),
    "company_admin": RoleDefinition(
        "Company administrator",
        frozenset(
            {
                "admin.user.manage",
                "admin.role.manage",
                "admin.company.manage",
                "ledger.journal.create",
                "ledger.journal.post",
                "ledger.period.close",
                "sales.invoice.create",
                "sales.invoice.post",
                "sales.credit_note.create",
                "sales.credit_note.post",
                "purchasing.bill.create",
                "purchasing.bill.post",
                "purchasing.debit_note.create",
                "purchasing.debit_note.post",
                "inventory.manage",
                "cashbank.manage",
                "cashbank.receipt.create",
                "cashbank.receipt.post",
                "cashbank.payment.create",
                "cashbank.payment.post",
                "cashbank.transfer.create",
                "cashbank.transfer.post",
                "cashbank.cheque.create",
                "cashbank.cheque.post",
                "cashbank.reconcile",
                "tax.manage",
                "report.view",
                "audit.view",
                "settings.manage",
            }
        ),
    ),
    "accountant": RoleDefinition(
        "Accountant",
        frozenset(
            {
                "ledger.journal.create",
                "ledger.journal.post",
                "ledger.period.close",
                "sales.invoice.create",
                "sales.invoice.post",
                "sales.credit_note.create",
                "sales.credit_note.post",
                "purchasing.bill.create",
                "purchasing.bill.post",
                "purchasing.debit_note.create",
                "purchasing.debit_note.post",
                "cashbank.receipt.create",
                "cashbank.receipt.post",
                "cashbank.payment.create",
                "cashbank.payment.post",
                "cashbank.transfer.create",
                "cashbank.transfer.post",
                "cashbank.cheque.create",
                "cashbank.cheque.post",
                "cashbank.reconcile",
                "tax.manage",
                "report.view",
            }
        ),
    ),
    "sales": RoleDefinition(
        "Sales clerk",
        frozenset(
            {
                "sales.invoice.create",
                "sales.invoice.post",
                "sales.credit_note.create",
                "sales.credit_note.post",
                "cashbank.receipt.create",
                "cashbank.receipt.post",
                "cashbank.cheque.create",
                "cashbank.cheque.post",
                "report.view",
            }
        ),
    ),
    "purchasing": RoleDefinition(
        "Purchasing clerk",
        frozenset(
            {
                "purchasing.bill.create",
                "purchasing.bill.post",
                "purchasing.debit_note.create",
                "purchasing.debit_note.post",
                "cashbank.payment.create",
                "cashbank.payment.post",
                "cashbank.cheque.create",
                "cashbank.cheque.post",
                "report.view",
            }
        ),
    ),
    "auditor": RoleDefinition("Auditor", frozenset({"report.view", "audit.view"})),
}


def permissions_for_roles(role_codes: Iterable[str]) -> frozenset[str]:
    """Return the union of permissions granted by the named default roles."""
    granted: set[str] = set()
    for code in role_codes:
        definition = DEFAULT_ROLES.get(code)
        if definition is not None:
            granted.update(definition.permissions)
    return frozenset(granted)
