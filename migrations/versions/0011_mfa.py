"""TOTP second factor for app users

Revision ID: 0011_mfa
Revises: 0010_settlements
Create Date: 2026-10-21 15:35:00

Phase 1 requires a second factor for privileged accounts. Each user can enrol
one TOTP secret; after enrolment every login must present a valid code. The
secret is stored in clear because the server must derive codes from it, and it
is only ever returned once during enrolment. The last accepted counter is kept
so a code cannot be replayed inside its 30-second window.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_mfa"
down_revision: str | Sequence[str] | None = "0010_settlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("totp_secret", sa.String(length=64), nullable=True))
    op.add_column(
        "app_user", sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("app_user", sa.Column("totp_last_counter", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "totp_last_counter")
    op.drop_column("app_user", "totp_enabled_at")
    op.drop_column("app_user", "totp_secret")
