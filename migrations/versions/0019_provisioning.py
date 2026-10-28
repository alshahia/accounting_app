"""Provisioning support: the per-user credential epoch.

Invitation and password-reset links are signed tokens, not stored rows. The
epoch makes them single-use: a link carries the user's current epoch, and
accepting it (or changing the password another way) increments the epoch, so
every older link stops verifying. This migration adds the column; no table is
needed for token storage.

Revision: 0019_provisioning
Down revision: 0018_stock_counts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_provisioning"
down_revision = "0018_stock_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the epoch column with a non-null default so existing users stay valid."""
    op.add_column(
        "app_user",
        sa.Column(
            "credential_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Drop the epoch column; outstanding links stop working."""
    op.drop_column("app_user", "credential_epoch")
