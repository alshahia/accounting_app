"""tax code jurisdiction

Revision ID: 0014_tax_jurisdiction
Revises: 0013_tax_rate_history
Create Date: 2026-10-22 11:10:00

v1 configures tax codes per jurisdiction (analysis/20 §5). Jurisdiction is
company data, not a code constant: it is free text such as IQ or SY, optional
because existing codes predate the field and a blank value stays valid.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_tax_jurisdiction"
down_revision: str | Sequence[str] | None = "0013_tax_rate_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the optional jurisdiction to tax codes."""
    op.add_column("tax_code", sa.Column("jurisdiction", sa.String(length=50), nullable=True))


def downgrade() -> None:
    """Drop the jurisdiction column."""
    op.drop_column("tax_code", "jurisdiction")
