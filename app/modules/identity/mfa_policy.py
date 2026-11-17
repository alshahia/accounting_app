"""MFA enforcement policy (analysis/21 §6).

Which actions need a second factor is security configuration, so the permission
groups live here as code while the tenant switches live in the tenant row. The
dependency calls :func:`mfa_required` before the permission check is satisfied.
"""

from __future__ import annotations

from app.modules.identity.permissions import ALL_PERMISSIONS

# User/role management and period close/reopen are the actions the
# specification makes mandatory even if a tenant would rather not ask.
SENSITIVE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "admin.user.manage",
        "admin.role.manage",
        "ledger.period.close",
    }
)

# Posting means changing the books: every create/post permission in the
# catalogue, derived so a new permission cannot be forgotten here.
POSTING_PERMISSIONS: frozenset[str] = frozenset(
    permission
    for permission in ALL_PERMISSIONS
    if permission.rsplit(".", 1)[-1] in {"create", "post"}
)


def mfa_required(
    permission: str,
    *,
    mfa_enabled: bool,
    mfa_sensitive_required: bool,
    mfa_posting_required: bool,
) -> bool:
    """Return whether this action is blocked until the user enrols TOTP."""
    if mfa_enabled:
        return False
    if mfa_sensitive_required and permission in SENSITIVE_PERMISSIONS:
        return True
    return mfa_posting_required and permission in POSTING_PERMISSIONS
