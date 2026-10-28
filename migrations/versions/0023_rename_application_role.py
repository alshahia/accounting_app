"""Rename the application role to the Al-Mahmoud name.

The product was renamed from its working name (Ameen) to Al-Mahmoud. Migration
0002 creates the cluster-wide NOLOGIN role that carries every table grant under
the working name. PostgreSQL keeps grants attached when a role is renamed (the
role OID does not change), so this migration only renames it; nothing else in
the chain needs to change. A fresh database runs 0002 first, so the old role is
always present when this migration runs, and a database that existed before the
rename is migrated in place.

Roles are cluster-wide while migrations are per database. A database created
*after* the rename replays the whole chain, so its 0002 creates a fresh
`ameen_app` while the cluster already holds the renamed `almahmoud_app`; the
rename is then impossible and the new database's grants still point at the old
role while the application connects as the new one. For that case (both roles
present) this migration copies the old role's privileges to the new role inside
this database and mirrors the default privileges, so a newly provisioned
database works without manual cluster surgery.

The database name and the login/superuser role are operator configuration, not
schema, so they are not renamed here; the release notes state the pilot action.
"""

from __future__ import annotations

from alembic import op

revision = "0023_rename_application_role"
down_revision = "0022_remove_backup_permission"
branch_labels = None
depends_on = None

# The names are constants, not user input: these are the two fixed role names.
UPGRADE_SQL = """
DO $$
DECLARE
    old_exists boolean;
    new_exists boolean;
    rec record;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameen_app') INTO old_exists;
    SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'almahmoud_app') INTO new_exists;

    IF old_exists AND NOT new_exists THEN
        -- The normal path: this database was created under the working name.
        ALTER ROLE ameen_app RENAME TO almahmoud_app;
    ELSIF old_exists AND new_exists THEN
        -- A sibling database already renamed the shared cluster role; copy this
        -- database's grants to the new name instead of renaming (impossible).
        GRANT USAGE ON SCHEMA public TO almahmoud_app;
        FOR rec IN
            SELECT table_schema, table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'ameen_app'
        LOOP
            EXECUTE format(
                'GRANT %s ON TABLE %I.%I TO almahmoud_app',
                rec.privilege_type, rec.table_schema, rec.table_name
            );
        END LOOP;
        FOR rec IN
            SELECT n.nspname AS sequence_schema, c.relname AS sequence_name,
                   a.privilege_type
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN LATERAL aclexplode(c.relacl) AS a
            JOIN pg_roles r ON r.oid = a.grantee
            WHERE c.relkind = 'S' AND r.rolname = 'ameen_app'
        LOOP
            EXECUTE format(
                'GRANT %s ON SEQUENCE %I.%I TO almahmoud_app',
                rec.privilege_type, rec.sequence_schema, rec.sequence_name
            );
        END LOOP;
        -- Future migrations rely on these default privileges.
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO almahmoud_app',
            current_user
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT USAGE, SELECT ON SEQUENCES TO almahmoud_app',
            current_user
        );
    END IF;
END
$$;
"""

DOWNGRADE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'almahmoud_app')
       AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ameen_app') THEN
        ALTER ROLE almahmoud_app RENAME TO ameen_app;
    END IF;
    -- When both roles exist the old name was recreated by a sibling database
    -- after the rename; the old role keeps this database's original grants, so
    -- there is nothing to move back. Left in place deliberately.
END
$$;
"""


def upgrade() -> None:
    """Rename the application role, keeping every grant attached."""
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    """Rename the application role back to the working name."""
    op.execute(DOWNGRADE_SQL)
