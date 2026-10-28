"""baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21 12:24:37.884619

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the schema change."""
    pass


def downgrade() -> None:
    """Revert the schema change."""
    pass
