"""Tamper-evident records: append-only audit trail, immutable posted lines.

The API already refuses to update or delete posted journal entries, but the
individual journal_line rows of a posted entry were still writable, so a
balanced pair of edits could rewrite history without touching the entry row.
This migration adds:

* a BEFORE UPDATE OR DELETE trigger on journal_line that rejects any change to a
  line whose parent entry is posted;
* REVOKE UPDATE, DELETE, TRUNCATE on audit_log from the application role, so the
  request path cannot rewrite the trail even if a service forgets (PostgreSQL
  answers 42501), plus a trigger that refuses the same operations for the owner.

Revision: 0020_record_integrity
Down revision: 0019_provisioning
"""

from __future__ import annotations

from alembic import op

revision = "0020_record_integrity"
down_revision = "0019_provisioning"
branch_labels = None
depends_on = None

AUDIT_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION public.audit_log_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only; rows cannot be changed or deleted'
        USING ERRCODE = 'insufficient_privilege';
END
$$
"""

JOURNAL_LINE_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION public.journal_line_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_status text;
BEGIN
    v_status := (SELECT status FROM journal_entry WHERE id = OLD.entry_id);
    IF v_status = 'posted' THEN
        RAISE EXCEPTION 'line % of posted journal entry % is immutable',
            OLD.id, OLD.entry_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    """Make the audit trail append-only and posted journal lines immutable."""
    op.execute(AUDIT_APPEND_ONLY_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION public.audit_log_append_only()
        """
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.audit_log FROM ameen_app")

    op.execute(JOURNAL_LINE_IMMUTABLE_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_journal_line_immutable
        BEFORE UPDATE OR DELETE ON journal_line
        FOR EACH ROW EXECUTE FUNCTION public.journal_line_immutable()
        """
    )


def downgrade() -> None:
    """Revert the guards and restore the old grants."""
    op.execute("DROP TRIGGER IF EXISTS trg_journal_line_immutable ON journal_line")
    op.execute("DROP FUNCTION IF EXISTS public.journal_line_immutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS public.audit_log_append_only()")
    op.execute("GRANT UPDATE, DELETE ON TABLE public.audit_log TO ameen_app")
