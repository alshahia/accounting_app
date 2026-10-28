"""Unit tests for the permission catalogue and default roles."""

from __future__ import annotations

from app.modules.identity.permissions import (
    ALL_PERMISSIONS,
    DEFAULT_ROLES,
    PERMISSION_DESCRIPTIONS,
    permissions_for_roles,
)


def test_catalogue_and_descriptions_agree() -> None:
    assert set(ALL_PERMISSIONS) == set(PERMISSION_DESCRIPTIONS)
    assert "ledger.journal.post" in ALL_PERMISSIONS


def test_default_roles_only_grant_catalogued_permissions() -> None:
    for code, role in DEFAULT_ROLES.items():
        assert role.name
        assert set(role.permissions) <= set(ALL_PERMISSIONS), code


def test_tenant_admin_has_every_permission() -> None:
    assert set(DEFAULT_ROLES["tenant_admin"].permissions) == set(ALL_PERMISSIONS)


def test_permissions_for_roles_unions_and_ignores_unknown() -> None:
    granted = permissions_for_roles({"accountant", "does_not_exist"})
    assert "ledger.journal.post" in granted
    assert "admin.user.manage" not in granted
    assert permissions_for_roles(set()) == frozenset()
