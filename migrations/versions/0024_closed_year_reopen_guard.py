"""Freeze a signed fiscal year: no period of a closed year can be reopened.

T-36's second layer. The ledger service refuses to reopen a period whose fiscal
year is closed (409 fiscal_year_closed); this migration adds the same refusal at
the database, so a direct SQL UPDATE cannot reopen one either. The guard is a
BEFORE UPDATE transition trigger on accounting_period: closed -> open is refused
when the owning fiscal year is closed. It fails closed as well when that fiscal
year row cannot be found, so a row-scoped visibility edge cannot bypass it.

The migration is DDL only: no backfill and no data rewrite. Inherited
inconsistent rows are not repaired here.

Pre-existing-data probe (recorded for the T-39 deploy checklist, which re-runs
both queries on a restored pilot dump and again after deploy):
* open periods whose fiscal year is closed
  => SELECT count(*) FROM accounting_period p JOIN fiscal_year y ON
     y.tenant_id = p.tenant_id AND y.company_id = p.company_id
     AND y.id = p.fiscal_year_id WHERE p.status = 'open' AND y.status = 'closed'
  observed on the dev cluster 2026-11-17: 0
* posted entries whose fiscal year is closed
  => SELECT count(*) FROM journal_entry e JOIN accounting_period p ON
     p.tenant_id = e.tenant_id AND p.company_id = e.company_id
     AND p.id = e.period_id JOIN fiscal_year y ON y.tenant_id = p.tenant_id
     AND y.company_id = p.company_id AND y.id = p.fiscal_year_id
     WHERE e.status = 'posted' AND y.status = 'closed'
  observed on the dev cluster 2026-11-17: 0

Revision: 0024_closed_year_reopen_guard
Down revision: 0023_rename_application_role
"""

from __future__ import annotations

from alembic import op

revision = "0024_closed_year_reopen_guard"
down_revision = "0023_rename_application_role"
branch_labels = None
depends_on = None

REOPEN_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION public.accounting_period_reopen_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_year_status text;
BEGIN
    IF OLD.status = 'closed' AND NEW.status = 'open' THEN
        v_year_status := (
            SELECT status
            FROM fiscal_year
            WHERE tenant_id = NEW.tenant_id
              AND company_id = NEW.company_id
              AND id = NEW.fiscal_year_id
        );
        IF v_year_status IS NULL OR v_year_status = 'closed' THEN
            RAISE EXCEPTION
                'accounting period % belongs to fiscal year % which is closed; reopening is refused',
                NEW.number, NEW.fiscal_year_id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    """Refuse closed -> open on a period whose fiscal year is closed."""
    op.execute(REOPEN_GUARD_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_accounting_period_reopen_guard
        BEFORE UPDATE ON accounting_period
        FOR EACH ROW EXECUTE FUNCTION public.accounting_period_reopen_guard()
        """
    )


def downgrade() -> None:
    """Drop the guard; periods become writable again."""
    op.execute("DROP TRIGGER IF EXISTS trg_accounting_period_reopen_guard ON accounting_period")
    op.execute("DROP FUNCTION IF EXISTS public.accounting_period_reopen_guard()")
